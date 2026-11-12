#!/usr/bin/env python3
"""Guarded, read-only worker-profile trial preparation and evidence ingestion.

The trial is instrumentation, not a benchmark: one frozen packet, one shared
Codex capacity snapshot, randomized launch order, and exact registered profiles. It
never executes a worker itself. ``prepare`` emits launch requests for a guarded
transport bridge; ``finalize`` validates returned telemetry and proves source
integrity. ``prepare-current`` binds the current strategy experiment's frozen
spec into a two-profile, three-instance plan when that spec is sealed, and
refuses when it is absent. It does not execute workers or write the program
summary. A measured finalize of that plan records the experiment id, work
shape, and spec hash beside quality, cost, and provider-resolved identity.
An explicit layout supports repeated instances of selected profiles;
verified evidence can be ingested atomically into a quarantine feedback database.
``publish-current`` records one already measured two-profile, three-instance
trial of the sealed strategy spec. It does not execute workers, and it refuses
when any instance has no token count. ``run-current`` is the live read-only
execution of that plan: six ``codex exec --sandbox read-only`` arms on the
frozen spec. Transport acknowledgement requires four binding lines in the final
message; recurring-work quality is scored separately from a read-only rubric
on the frozen specification text. The rubric checks the positions and word
counts of up to two specification lines. Cost is token counts from
the session stream. ``response.completed`` and ``turn.completed.server_model``
provide unverified model claims. Verified ingestion additionally requires a
provider response retrieved through an independently trusted runtime lookup;
session fields and artifact digests cannot establish that provenance. The CLI
has no trusted provider collector and records session measurements as unverified
without a verified-ingest marker. Sessions without model claims are refused.
``--selftest`` exercises
prepare, validation, and reporting fully offline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable
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
# The task's comparison is two Codex profiles, three instances each. Astra is the full tier
# and Terra the mid tier; both share the one Codex subscription pool. Luna stays on the
# three-arm set above and is not part of this layout.
CURRENT_TRIAL_PROFILE_IDS = (
    "codex-6-astra-high",
    "codex-5.6-terra-high",
)
CURRENT_TRIAL_INSTANCES = 3
_EXPERIMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _manifest_profile_ids(manifest: dict[str, Any]) -> tuple[str, ...]:
    requests = manifest.get("requests") or []
    actual = {item.get("profile_id") for item in requests}
    if "instances_per_profile" in manifest and "selected_profile_ids" not in manifest:
        raise ValueError("trial instance layout requires selected profiles")
    if "selected_profile_ids" in manifest:
        selected = manifest["selected_profile_ids"]
        count = manifest.get("instances_per_profile")
        supported_ids = set(EXPECTED_PROFILE_IDS) | set(LEGACY_PROFILE_IDS)
        if (
            not isinstance(selected, list)
            or not all(isinstance(pid, str) for pid in selected)
            or len(selected) < 2
            or len(set(selected)) != len(selected)
            or not set(selected) <= supported_ids
            or type(count) is not int
            or not 1 <= count <= 3
        ):
            raise ValueError("trial requires supported profiles and one to three instances")
        if actual != set(selected) or any(
            sum(item.get("profile_id") == pid for item in requests) != count for pid in selected
        ):
            raise ValueError("trial requests do not match the selected profile instance counts")
        return tuple(selected)
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
UNVERIFIED_SESSION_AUTHORITY = "codex-session-unverified/v1"
# Accepted evidence formats, not a source of provider trust.
IDENTITY_AUTHORITIES = {
    "workflows-read-only-trial-artifact/v1",
    "openai-response-metadata/v1",
    UNVERIFIED_SESSION_AUTHORITY,
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
    profile_ids: Iterable[str] | None = None,
    instances_per_profile: int = 1,
) -> dict[str, Any]:
    """Build a frozen, randomized, source-integrity-bound trial plan."""
    timestamp = int(time.time()) if now is None else int(now)
    roots = (orchestrator_root.resolve(), workflows_root.resolve())
    selected = tuple(EXPECTED_PROFILE_IDS if profile_ids is None else profile_ids)
    if type(instances_per_profile) is not int or not 1 <= instances_per_profile <= 3:
        raise ValueError("trial requires one to three instances per profile")
    custom_layout = profile_ids is not None or instances_per_profile != 1
    if custom_layout:
        _manifest_profile_ids(
            {
                "selected_profile_ids": list(selected),
                "instances_per_profile": instances_per_profile,
                "requests": [
                    {"profile_id": pid} for pid in selected for _ in range(instances_per_profile)
                ],
            }
        )
    profiles = [execution_profiles.get_profile(profile_id) for profile_id in selected]
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

    frozen_packet = json.loads(json.dumps(FROZEN_PACKET if packet is None else packet))
    packet_hash = _sha256(frozen_packet)
    source_before = {
        "orchestrator": source_manifest(roots[0]),
        "workflows": source_manifest(roots[1]),
    }
    order = [
        (pid, instance) for pid in selected for instance in range(1, instances_per_profile + 1)
    ]
    random.Random(int(seed)).shuffle(order)
    identity = {
        "created_at": timestamp,
        "packet_hash": packet_hash,
        "profile_ids": list(selected),
        "seed": int(seed),
        "source_hashes": {key: value["aggregate_sha256"] for key, value in source_before.items()},
    }
    if custom_layout:
        identity["instances_per_profile"] = instances_per_profile
    trial_id = "model-profile-trial:" + _sha256(identity).split(":", 1)[1][:24]
    pool_snapshot = _shared_pool_snapshot(state=capacity_state, captured_at=timestamp)
    requests = []
    for launch_ordinal, (profile_id, instance) in enumerate(order, start=1):
        profile = execution_profiles.get_profile(profile_id)
        request = {
            "run_id": f"{trial_id}:{profile_id}"
            + (f":instance:{instance}" if instances_per_profile > 1 else ""),
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
        # The pinned runner accepts only ordinals 1..3. Repeated instances keep
        # that per-profile index beside the global launch position, which runs
        # past 3 once two profiles are each repeated.
        if instances_per_profile > 1:
            request["instance_ordinal"] = instance
        requests.append(request)
    manifest = {
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
        "launch_order": [pid for pid, _ in order],
        "requests": requests,
        "capacity_snapshot": pool_snapshot,
        "source_before": source_before,
        "stop_conditions": {
            "triggered": bool(stop_reasons),
            "reasons": stop_reasons,
        },
    }
    if custom_layout:
        manifest["selected_profile_ids"] = list(selected)
        manifest["instances_per_profile"] = instances_per_profile
    return manifest


class RecurringWorkUnavailable(ValueError):
    """The current strategy experiment has no sealed frozen spec to reuse."""


def _experiments_root(experiments_root: Path | None) -> Path:
    if experiments_root is not None:
        return experiments_root
    override = os.environ.get("ORCH_EXP_DIR")
    if override:
        return Path(override)
    import exp_abcd

    return exp_abcd.EXP_DIR


def resolve_strategy_frozen_packet(
    *,
    state_dir: Path | None = None,
    experiments_root: Path | None = None,
) -> dict[str, Any]:
    """Load the current strategy experiment's frozen spec as a trial packet.

    The spec is available only when the capability-program receipt names one
    experiment, that experiment's ``strategy.json`` points at its own ``spec.md``,
    and the text is not a recovered missing-spec stub. A recorded ``spec_sha256``
    must match the file. There is no fallback packet: a generic instrumentation
    packet is a different work shape.
    """
    import spec_provenance
    import strategy_experiment

    receipt_path = strategy_experiment.strategy_result_path(state_dir)
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RecurringWorkUnavailable(
            f"strategy experiment receipt is absent: {receipt_path}"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RecurringWorkUnavailable(
            f"strategy experiment receipt is unreadable: {receipt_path}"
        ) from exc
    if not isinstance(receipt, dict):
        raise RecurringWorkUnavailable("strategy experiment receipt is not an object")
    exp_id = receipt.get("exp_id")
    if not isinstance(exp_id, str) or not _EXPERIMENT_ID_RE.fullmatch(exp_id):
        raise RecurringWorkUnavailable("strategy experiment receipt has no experiment id")
    experiment = _experiments_root(experiments_root) / exp_id
    spec_path = (experiment / "spec.md").resolve()
    metadata_path = experiment / "strategy.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RecurringWorkUnavailable(
            f"strategy experiment metadata is absent: {metadata_path}"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RecurringWorkUnavailable(
            f"strategy experiment metadata is unreadable: {metadata_path}"
        ) from exc
    if not isinstance(metadata, dict) or not isinstance(metadata.get("spec_file"), str):
        raise RecurringWorkUnavailable("strategy experiment metadata has no spec_file")
    recorded = Path(metadata["spec_file"]).expanduser()
    recorded_path = recorded if recorded.is_absolute() else experiment / recorded
    try:
        if recorded_path.resolve() != spec_path or not spec_path.is_file():
            raise RecurringWorkUnavailable(
                "strategy experiment spec_file is not the frozen spec.md"
            )
        spec_text = spec_path.read_text(encoding="utf-8")
    except RecurringWorkUnavailable:
        raise
    except (OSError, UnicodeError) as exc:
        raise RecurringWorkUnavailable(
            f"strategy experiment frozen spec is unreadable: {spec_path}"
        ) from exc
    meta: dict[str, Any] = {}
    meta_path = experiment / "meta.json"
    if meta_path.exists():
        try:
            loaded = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecurringWorkUnavailable(
                f"strategy experiment meta.json is unreadable: {meta_path}"
            ) from exc
        if not isinstance(loaded, dict):
            raise RecurringWorkUnavailable("strategy experiment meta.json is not an object")
        meta = loaded
        sealed = meta.get("spec_sha256")
        if sealed is not None:
            actual = hashlib.sha256(spec_text.encode("utf-8")).hexdigest()
            if not isinstance(sealed, str) or sealed != actual:
                raise RecurringWorkUnavailable(
                    "strategy experiment spec_sha256 does not match spec.md"
                )
    if not spec_text.strip() or spec_provenance.is_missing_spec(meta, spec_text):
        raise RecurringWorkUnavailable("strategy experiment frozen spec is a missing-spec stub")
    return {
        "schema": "orchestrator.model-profile-trial-packet",
        "version": 1,
        "purpose": "compare two Codex profiles on one frozen recurring-work spec",
        "work_shape": "strategy-experiment-frozen-spec",
        "exp_id": exp_id,
        "spec_sha256": "sha256:" + hashlib.sha256(spec_text.encode("utf-8")).hexdigest(),
        "instruction": (
            "Read this frozen recurring-work specification without changing files. "
            "Return only the bounded identity fields requested by the Workflows "
            "worker telemetry contract."
        ),
        "frozen_spec": spec_text,
        "expected_output": {
            "acknowledged": True,
            "packet_hash": "supplied-by-manifest",
        },
    }


def build_current_recurring_trial(
    orchestrator_root: Path,
    workflows_root: Path,
    *,
    seed: int = 56014,
    now: int | None = None,
    capacity_state: str = "unknown",
    state_dir: Path | None = None,
    experiments_root: Path | None = None,
) -> dict[str, Any]:
    """Plan two Codex profiles × three instances on the current frozen spec.

    This builds the launch plan only. Workers stay on the guarded transport,
    and the capability-program summary is written by ``finalize`` from measured
    attempts, never from this plan.
    """
    packet = resolve_strategy_frozen_packet(state_dir=state_dir, experiments_root=experiments_root)
    return build_trial_manifest(
        orchestrator_root,
        workflows_root,
        seed=seed,
        now=now,
        capacity_state=capacity_state,
        packet=packet,
        profile_ids=CURRENT_TRIAL_PROFILE_IDS,
        instances_per_profile=CURRENT_TRIAL_INSTANCES,
    )


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
    instances = manifest.get("instances_per_profile", 1)
    expected_count = len(profile_ids) * instances
    if manifest.get("trial_id") != _expected_trial_id(manifest):
        raise ValueError("trial identity is not reproducible")

    requests = manifest.get("requests") or []
    if {item.get("profile_id") for item in requests} != set(profile_ids):
        raise ValueError("trial requires an exact supported profile set")
    if len({item.get("run_id") for item in requests}) != expected_count:
        raise ValueError("trial requires one distinct run identity per instance")
    if manifest.get("capacity_snapshot", {}).get("snapshot_count") != 1:
        raise ValueError("trial requires one shared-pool snapshot")
    launch_order = manifest.get("launch_order") or []
    if sorted(launch_order) != sorted(pid for pid in profile_ids for _ in range(instances)):
        raise ValueError("trial launch order is not an exact profile permutation")
    by_ordinal = sorted(requests, key=lambda item: int(item.get("launch_ordinal") or 0))
    if [item.get("profile_id") for item in by_ordinal] != launch_order:
        raise ValueError("trial request ordinals do not match launch order")
    if [int(item.get("launch_ordinal") or 0) for item in by_ordinal] != list(
        range(1, expected_count + 1)
    ):
        raise ValueError("trial launch ordinals must cover the selected profile set")
    if instances > 1:
        for profile_id in profile_ids:
            ordinals = [
                item.get("instance_ordinal")
                for item in requests
                if item.get("profile_id") == profile_id
            ]
            if any(type(value) is not int for value in ordinals) or sorted(ordinals) != list(
                range(1, instances + 1)
            ):
                raise ValueError("trial instance ordinals must cover 1..N within each profile")
            for item in requests:
                if item.get("profile_id") != profile_id:
                    continue
                expected_run = (
                    f"{manifest['trial_id']}:{profile_id}:instance:{item['instance_ordinal']}"
                )
                if item.get("run_id") != expected_run:
                    raise ValueError("trial instance ordinal does not match its run identity")
    expected_run_ids = {
        f"{manifest['trial_id']}:{pid}" + (f":instance:{i}" if instances > 1 else "")
        for pid in profile_ids
        for i in range(1, instances + 1)
    }
    if {item.get("run_id") for item in requests} != expected_run_ids:
        raise ValueError("trial request run identity is not reproducible")
    for request in requests:
        profile = execution_profiles.get_profile(request["profile_id"])
        profile_run_ids = {
            f"{manifest['trial_id']}:{request['profile_id']}"
            + (f":instance:{i}" if instances > 1 else "")
            for i in range(1, instances + 1)
        }
        if request.get("run_id") not in profile_run_ids:
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
    by_run = {item.get("run_id"): item for item in attempts}
    request_by_run = {item["run_id"]: item for item in manifest["requests"]}
    if set(by_run) != set(request_by_run) or len(attempts) != len(request_by_run):
        raise ValueError("trial results require exactly one attempt per instance")
    for run_id, attempt in by_run.items():
        unknown_attempt = sorted(set(attempt) - ATTEMPT_FIELDS)
        if unknown_attempt:
            raise ValueError(f"trial attempt contains unsupported fields: {unknown_attempt}")
        request = request_by_run[run_id]
        if attempt.get("profile_id") != request["profile_id"]:
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
        for field in ("tokens_in", "tokens_out"):
            value = attempt.get(field)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"trial attempt {field} must be a nonnegative integer or null")
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
            raise ValueError("trial attempt identity authority is unsupported")
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
        {key: by_run[request["run_id"]].get(key) for key in sorted(ATTEMPT_FIELDS)}
        for request in sorted(
            manifest["requests"],
            key=lambda row: (profile_ids.index(row["profile_id"]), row["run_id"]),
        )
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


def _expected_trial_id(manifest: dict[str, Any]) -> str:
    """Trial id is the hash of the frozen packet, profiles, seed, and source seals."""
    source_before = manifest["source_before"]
    profile_ids = _manifest_profile_ids(manifest)
    identity = {
        "created_at": manifest.get("created_at"),
        "packet_hash": manifest.get("packet_hash"),
        "profile_ids": list(profile_ids),
        "seed": manifest.get("seed"),
        "source_hashes": {
            key: source_before[key]["aggregate_sha256"] for key in ("orchestrator", "workflows")
        },
    }
    if "selected_profile_ids" in manifest:
        identity["instances_per_profile"] = manifest.get("instances_per_profile", 1)
    return "model-profile-trial:" + _sha256(identity).split(":", 1)[1][:24]


def _require_consistent_source_seal(manifest: dict[str, Any]) -> None:
    """Each recorded aggregate must be the hash of its own entry list.

    The live-tree comparison trusts that aggregate. Rewriting it to a mutated
    tree's hash, leaving the old entries in place, and rebinding ``trial_id``
    would otherwise look unchanged and let ``finalize --ingest`` proceed.
    """
    for name, recorded in manifest["source_before"].items():
        entries = recorded.get("entries") if isinstance(recorded, dict) else None
        sealed = recorded.get("aggregate_sha256") if isinstance(recorded, dict) else None
        if not isinstance(entries, list) or sealed != _sha256(entries):
            raise ValueError(f"trial source manifest aggregate does not match its entries:{name}")


def _authoritative_identity_verified(
    attempts: list[dict[str, Any]],
    *,
    provider_response_lookup: Callable[[str], dict[str, Any] | None] | None = None,
) -> bool:
    """Bind local measurements to a response obtained through a trusted channel.

    The lookup is supplied by trusted application code, never by the result JSON.
    It must authenticate provider provenance independently of the session and its
    artifacts. There is currently no such collector in the CLI, so it fails closed.
    A response-shaped object, authority label, or digest is not provenance.
    """
    if not attempts or provider_response_lookup is None:
        return False
    response_ids: set[str] = set()
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
        try:
            raw = artifact_path.read_bytes()
            if len(raw) > 64 * 1024:
                return False
            actual_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
            if actual_hash != expected_hash:
                return False
            artifact = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not isinstance(artifact, dict):
            return False
        # A digest alone binds bytes, not worker identity. Require the recorded
        # identity content to agree with every provider-resolved attempt field.
        for field in ("profile_id", "provider_resolved_provider", "provider_resolved_model"):
            if not attempt.get(field) or artifact.get(field) != attempt[field]:
                return False
        response_id = artifact.get("response_id")
        if (
            artifact.get("object") != "response"
            or not isinstance(response_id, str)
            or not re.fullmatch(r"resp_[A-Za-z0-9]+", response_id)
            or response_id in response_ids
            or attempt.get("provider_resolved_provider") != "openai"
        ):
            return False
        response_ids.add(response_id)
        response = provider_response_lookup(response_id)
        if not isinstance(response, dict):
            return False
        if any(
            response.get(field) != expected
            for field, expected in {
                "object": "response",
                "id": response_id,
                "status": "completed",
                "model": attempt["provider_resolved_model"],
                "metadata": {
                    "run_id": attempt.get("run_id"),
                    "profile_id": attempt["profile_id"],
                    "packet_hash": attempt.get("packet_hash"),
                },
            }.items()
        ):
            return False
        usage = response.get("usage")
        if not isinstance(usage, dict):
            return False
        for attempt_field, usage_field in (
            ("tokens_in", "input_tokens"),
            ("tokens_out", "output_tokens"),
        ):
            value = usage.get(usage_field)
            if type(value) is not int or value < 0 or value != attempt.get(attempt_field):
                return False
    return True


def _require_verified_identity_for_ingest(
    results: dict[str, Any],
    *,
    identity_verified: bool,
) -> None:
    if results.get("identity_verified") is not True:
        raise ValueError("trial ingest refused: worker identity is unverified")
    if not identity_verified:
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


def _trial_token_total(attempts: list[dict[str, Any]], field: str) -> int | str:
    """Sum measured usage only when every instance supplied that direction."""
    values = [attempt.get(field) for attempt in attempts]
    if not values or any(value is None for value in values):
        return "n/a"
    return sum(value for value in values if value is not None)


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
    tokens_in = _trial_token_total(attempts, "tokens_in")
    tokens_out = _trial_token_total(attempts, "tokens_out")
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
                field: _trial_token_total([a for a in attempts if a["profile_id"] == pid], field)
                for field in ("tokens_in", "tokens_out")
            }
            for pid in profile_ids
        },
        "provider_resolved_identity_by_profile": {
            pid: {
                "provider": next(a for a in attempts if a["profile_id"] == pid)[
                    "provider_resolved_provider"
                ],
                "model": next(a for a in attempts if a["profile_id"] == pid)[
                    "provider_resolved_model"
                ],
            }
            for pid in profile_ids
        },
        "aggregate_tokens_in": tokens_in,
        "aggregate_tokens_out": tokens_out,
    }
    # The plan refuses to invent this file. Once measured attempts publish it, the
    # frozen spec that selected the work shape has to be on the file: quality and
    # cost without the experiment id are not identifiable as that recurring trial.
    packet = manifest.get("frozen_packet")
    if isinstance(packet, dict):
        for field in ("work_shape", "exp_id", "spec_sha256"):
            value = packet.get(field)
            if isinstance(value, str) and value:
                payload[field] = value
    _atomic_json(out_dir / "profile-trial.json", payload)


def recurring_trial_prompt(manifest: dict[str, Any], request: dict[str, Any]) -> str:
    """The frozen spec, transport acknowledgement lines, and a bounded read-only work task."""
    packet = manifest.get("frozen_packet")
    if not isinstance(packet, dict):
        raise ValueError("recurring trial prompt requires the frozen packet")
    spec = packet.get("frozen_spec")
    spec_sha = packet.get("spec_sha256")
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError("recurring trial prompt requires the frozen spec text")
    if not isinstance(spec_sha, str) or not spec_sha.startswith("sha256:"):
        raise ValueError("recurring trial prompt requires the frozen spec hash")
    lines = (
        f"packet_hash={manifest['packet_hash']}",
        f"spec_sha256={spec_sha}",
        f"profile_id={request['profile_id']}",
        f"run_id={request['run_id']}",
    )
    instruction = packet.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        instruction = "Read this frozen recurring-work specification without changing files."
    return (
        instruction.rstrip()
        + "\nDo not edit, create, commit, or push files.\n"
        + "First, return exactly these four lines (transport acknowledgement), then a blank line, "
        "then one line of recurring-work evidence.\n"
        + "Number the frozen specification's physical lines starting at 1, including blanks. "
        'Find the first line starting with "Acceptance criteria:" (case-insensitive, '
        "ignoring indentation). Select its text after the colon if nonempty, then successive "
        "nonblank lines below it up to a hash-style Markdown heading (one to six # "
        "characters followed by whitespace or end of line), for at most two selections. "
        "Skip blank lines before the first selection and stop at a blank after it. "
        "If the criteria block supplies no text or there is no label, select the first "
        "two nonblank lines other than the empty label. Count whitespace-separated tokens "
        "in each selection, including any standalone list markers. Return "
        "`work_evidence=` followed by comma-separated `line_number:word_count` pairs "
        "in source order, with no spaces.\n"
        + "Do not add any other text.\n"
        + "\n".join(lines)
        + "\n\nFrozen specification:\n"
        + spec
    )


def _recurring_work_rubric(spec: str) -> str:
    """Expected line-number and word-count pairs for the bounded spec task."""
    lines = [(number, line.strip()) for number, line in enumerate(spec.splitlines(), 1)]
    label = "acceptance criteria:"
    criteria = []
    for index, (number, line) in enumerate(lines):
        if not line.lower().startswith(label):
            continue
        inline = line[len(label) :].strip()
        if inline:
            criteria.append((number, inline))
        for next_number, continuation in lines[index + 1 :]:
            if len(criteria) == 2 or re.match(r"#{1,6}(?:\s|$)", continuation):
                break
            if continuation:
                criteria.append((next_number, continuation))
            elif criteria:
                break
        break
    selected = (
        criteria or [(number, line) for number, line in lines if line and line.lower() != label][:2]
    )
    return ",".join(f"{number}:{len(line.split())}" for number, line in selected)


def _work_quality_score(manifest: dict[str, Any], final_text: str) -> float:
    """Score a single evidence line against the exact computed answer."""
    packet = manifest.get("frozen_packet")
    if not isinstance(packet, dict):
        return 0.0
    spec = packet.get("frozen_spec")
    if not isinstance(spec, str) or not spec.strip():
        return 0.0
    rubric = _recurring_work_rubric(spec)
    prefix = "work_evidence="
    answers = [
        line[len(prefix) :]
        for raw in (final_text or "").splitlines()
        if (line := raw.strip()).startswith(prefix)
    ]
    return float(bool(rubric) and answers == [rubric])


def _acknowledgement_score(
    manifest: dict[str, Any], request: dict[str, Any], final_text: str
) -> tuple[bool, float]:
    """Transport acknowledgement (four lines) and recurring-work quality (rubric), separately.

    All four binding lines must appear for acknowledgement. Work quality is 1.0
    only when ``work_evidence=`` supplies the computed line-number and word-count pairs.
    """
    packet = manifest["frozen_packet"]
    required = (
        f"packet_hash={manifest['packet_hash']}",
        f"spec_sha256={packet['spec_sha256']}",
        f"profile_id={request['profile_id']}",
        f"run_id={request['run_id']}",
    )
    text = final_text or ""
    acknowledged = all(line in text for line in required)
    return acknowledged, _work_quality_score(manifest, text)


def _jsonl_objects(session_text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line_number, raw in enumerate(session_text.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"codex session is not JSONL at line {line_number}") from exc
        if isinstance(value, dict):
            events.append(value)
    return events


def _optional_provider_response(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """One OpenAI response object, or none when the stream has no such event."""
    found: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "response.completed":
            continue
        response = event.get("response")
        if not isinstance(response, dict) or response.get("object") != "response":
            raise ValueError("codex response metadata is not an OpenAI response object")
        model = response.get("model")
        response_id = response.get("id")
        if type(model) is not str or not model.strip():
            raise ValueError("codex response metadata has no model")
        if type(response_id) is not str or not response_id.startswith("resp_"):
            raise ValueError("codex response metadata has no OpenAI response id")
        usage = response.get("usage")
        found.append(
            {
                "model": model,
                "id": response_id,
                "usage": usage if isinstance(usage, dict) else None,
            }
        )
    if not found:
        return None
    if len(found) != 1:
        raise ValueError("codex session has multiple provider responses")
    return found[0]


def _one_provider_response(events: list[dict[str, Any]]) -> dict[str, Any]:
    """The OpenAI response object, never the CLI's configured model.

    ``turn_context`` is what the CLI was asked to run. Subscription sessions
    have no separate provider identity unless the stream carries one response
    object (``object`` of ``response`` and an id beginning with ``resp_``).
    """
    found = _optional_provider_response(events)
    if found is None:
        raise ValueError("provider_resolved_identity_unavailable")
    return found


def _completed_server_model(events: list[dict[str, Any]]) -> str | None:
    """Provider model copied onto ``turn.completed`` as ``server_model``.

    A generic ``model`` field on that event is the CLI's configured name and is
    ignored. ``null`` means the provider omitted the id. Two non-null values
    are a conflict even when they match.
    """
    found: list[str] = []
    for event in events:
        if event.get("type") != "turn.completed" or "server_model" not in event:
            continue
        value = event.get("server_model")
        if value is None:
            continue
        if type(value) is not str or not value.strip():
            raise ValueError("codex server_model is not a provider model id")
        found.append(value)
    if not found:
        return None
    if len(found) != 1:
        raise ValueError("codex session has multiple provider models")
    return found[0]


def _provider_witness(events: list[dict[str, Any]]) -> dict[str, Any]:
    """The served model, from response metadata or ``server_model``, not argv."""
    response = _optional_provider_response(events)
    server_model = _completed_server_model(events)
    if response is not None and server_model is not None and response["model"] != server_model:
        raise ValueError("codex response model disagrees with server_model")
    if response is not None:
        return {
            "model": response["model"],
            "id": response["id"],
            "usage": response["usage"],
            "source": "openai-response",
        }
    if server_model is not None:
        return {"model": server_model, "id": None, "usage": None, "source": "server_model"}
    raise ValueError("provider_resolved_identity_unavailable")


def _one_cli_model(events: list[dict[str, Any]]) -> str | None:
    models: set[str] = set()
    for event in events:
        if event.get("type") != "turn_context":
            continue
        payload = event.get("payload")
        model = payload.get("model") if isinstance(payload, dict) else None
        if type(model) is str and model.strip():
            models.add(model)
    if len(models) > 1:
        raise ValueError("codex session reports multiple CLI models")
    return next(iter(models), None)


def _nonnegative_pair(usage: dict[str, Any] | None, label: str) -> tuple[int, int] | None:
    if usage is None:
        return None
    tokens_in = usage.get("input_tokens")
    tokens_out = usage.get("output_tokens")
    if tokens_in is None and tokens_out is None:
        return None
    if type(tokens_in) is not int or type(tokens_out) is not int or tokens_in < 0 or tokens_out < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return tokens_in, tokens_out


def _measured_usage(
    response_usage: dict[str, Any] | None, events: list[dict[str, Any]]
) -> tuple[int, int]:
    from_response = _nonnegative_pair(response_usage, "codex response usage")
    turn_pairs: list[tuple[int, int]] = []
    for event in events:
        if event.get("type") != "turn.completed" or not isinstance(event.get("usage"), dict):
            continue
        pair = _nonnegative_pair(event["usage"], "codex turn usage")
        if pair is not None:
            turn_pairs.append(pair)
    if len(turn_pairs) > 1:
        raise ValueError("codex session has multiple turn usage records")
    from_turn = turn_pairs[0] if turn_pairs else None
    if from_response is not None and from_turn is not None and from_response != from_turn:
        raise ValueError("codex response usage disagrees with turn usage")
    measured = from_response if from_response is not None else from_turn
    if measured is None:
        raise ValueError("measured recurring trial tokens_in must be a nonnegative integer")
    return measured


def measure_codex_session(
    manifest: dict[str, Any],
    request: dict[str, Any],
    session_text: str,
    final_text: str,
) -> dict[str, Any]:
    """Measure one read-only arm. The recorded model is the provider witness."""
    events = _jsonl_objects(session_text)
    metadata = _provider_witness(events)
    cli_model = _one_cli_model(events)
    if cli_model is not None and cli_model != metadata["model"]:
        raise ValueError("codex CLI model disagrees with the provider response")
    if request.get("provider") != "openai":
        raise ValueError("provider response metadata is not an OpenAI response")
    if metadata["model"] != request.get("requested_model"):
        raise ValueError("provider served a different model than the profile requested")
    acknowledged, quality = _acknowledgement_score(manifest, request, final_text)
    if not acknowledged:
        raise ValueError("codex arm did not acknowledge the frozen packet")
    tokens_in, tokens_out = _measured_usage(metadata.get("usage"), events)
    return {
        "model": metadata["model"],
        "response_id": metadata["id"],
        "identity_source": metadata["source"],
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "quality": quality,
        "provider": "openai",
    }


def _identity_artifact(directory: Path, request: dict[str, Any], measured: dict[str, Any]) -> dict:
    payload: dict[str, Any] = {
        "provenance": UNVERIFIED_SESSION_AUTHORITY,
        "run_id": request["run_id"],
        "packet_hash": request["packet_hash"],
        "profile_id": request["profile_id"],
        "provider_resolved_model": measured["model"],
        "provider_resolved_provider": measured["provider"],
        "tokens_in": measured["tokens_in"],
        "tokens_out": measured["tokens_out"],
    }
    if measured.get("response_id"):
        payload["object"] = "response"
        payload["response_id"] = measured["response_id"]
    else:
        # This is a session claim, not an independently authenticated response.
        payload["object"] = "turn.completed"
        payload["server_model"] = measured["model"]
    path = directory / f"identity-{int(request['launch_ordinal'])}.json"
    _atomic_json(path, payload)
    digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "schema": IDENTITY_EVIDENCE_SCHEMA,
        "version": IDENTITY_EVIDENCE_VERSION,
        "authority": UNVERIFIED_SESSION_AUTHORITY,
        "artifact_ref": str(path),
        "artifact_sha256": digest,
    }


def results_from_codex_sessions(
    manifest: dict[str, Any],
    sessions: list[dict[str, Any]],
    *,
    artifact_dir: Path,
    cli_version: str,
    runner_version: str,
) -> dict[str, Any]:
    """Build explicitly unverified measurements from session transcripts.

    Session fields are useful measurements, but cannot authenticate their own
    provider provenance. Writes nothing when measurement validation refuses.
    """
    by_run = {row.get("run_id"): row for row in sessions}
    requests = list(manifest.get("requests") or [])
    if set(by_run) != {row["run_id"] for row in requests} or len(sessions) != len(requests):
        raise ValueError("measured recurring trial requires six attempts")
    measured_by_run: dict[str, dict[str, Any]] = {}
    for request in requests:
        session = by_run[request["run_id"]]
        if not isinstance(session, dict):
            raise ValueError("measured recurring trial attempt is not an object")
        measured_by_run[request["run_id"]] = measure_codex_session(
            manifest,
            request,
            str(session.get("session_text") or ""),
            str(session.get("final_text") or ""),
        )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    attempts = []
    qualities: dict[str, list[float]] = {}
    for request in requests:
        measured = measured_by_run[request["run_id"]]
        qualities.setdefault(request["profile_id"], []).append(float(measured["quality"]))
        attempts.append(
            {
                "run_id": request["run_id"],
                "profile_id": request["profile_id"],
                "operation_role": "worker",
                "requested_model": request["requested_model"],
                "selected_model": measured["model"],
                "reported_model": measured["model"],
                "provider_resolved_provider": measured["provider"],
                "provider_resolved_model": measured["model"],
                "fallback_reason": None,
                "runner_version": runner_version,
                "cli_version": cli_version,
                "status": "success",
                "tokens_in": measured["tokens_in"],
                "tokens_out": measured["tokens_out"],
                "packet_hash": manifest["packet_hash"],
                "acknowledged": True,
                "identity_evidence": _identity_artifact(artifact_dir, request, measured),
            }
        )
    return {
        "schema": RESULT_SCHEMA,
        "version": SCHEMA_VERSION,
        "trial_id": manifest["trial_id"],
        "packet_hash": manifest["packet_hash"],
        "acknowledged": True,
        "identity_verified": False,
        "attempts": attempts,
        "quality_by_profile": {
            profile_id: sum(scores) / len(scores) for profile_id, scores in qualities.items()
        },
    }


def codex_read_only_argv(
    binary: Path, request: dict[str, Any], prompt: str, output_path: Path
) -> list[str]:
    """Argv for one arm. The prompt is an argument; identity is not read from it."""
    if len(prompt) > 200_000:
        raise ValueError("frozen spec is too large to pass to the read-only runner")
    return [
        str(binary),
        "exec",
        "--json",
        "--ignore-user-config",
        "--strict-config",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--model",
        str(request["requested_model"]),
        "-c",
        'model_reasoning_effort="high"',
        "--output-last-message",
        str(output_path),
        prompt,
    ]


def execute_codex_read_only(
    binary: Path, request: dict[str, Any], prompt: str, workdir: Path
) -> tuple[str, str, str]:
    """Run one sandboxed arm. Returns the JSONL stream, final message, and CLI version.

    Provider identity and usage are interpreted only from ``stdout`` (JSONL). The
    ``--model`` argv names the profile contract; it is not treated as provider
    resolution. ``turn.completed.server_model`` is also accepted when the CLI
    copied it from provider response metadata. A session whose JSONL has usage
    and only ``turn_context`` or a generic ``model`` field still refuses with
    ``provider_resolved_identity_unavailable``, which is what current
    subscription ``codex exec --json`` streams do.
    """
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError(f"codex read-only runner is absent: {binary}")
    workdir.mkdir(parents=True, exist_ok=True)
    final_path = workdir / "final.txt"
    version = subprocess.run(
        [str(binary), "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    cli_version = " ".join((version.stdout or "").split())
    if version.returncode != 0 or not cli_version:
        raise ValueError("codex read-only runner did not report a version")
    argv = codex_read_only_argv(binary, request, prompt, final_path)
    if "--sandbox" not in argv or argv[argv.index("--sandbox") + 1] != "read-only":
        raise ValueError("codex arm refused: sandbox is not read-only")
    completed = subprocess.run(
        argv,
        cwd=workdir,
        check=False,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if completed.returncode != 0:
        raise ValueError("codex read-only arm failed")
    if not final_path.is_file():
        raise ValueError("codex read-only arm wrote no final message")
    return completed.stdout, final_path.read_text(encoding="utf-8"), cli_version


def run_current_recurring_trial(
    orchestrator_root: Path,
    workflows_root: Path,
    *,
    seed: int = 56014,
    now: int | None = None,
    capacity_state: str = "unknown",
    state_dir: Path | None = None,
    experiments_root: Path | None = None,
    state_path: Path | None = None,
    artifact_dir: Path | None = None,
    codex_bin: Path | None = None,
    execute: Callable[[dict[str, Any], str], tuple[str, str, str]] | None = None,
) -> dict[str, Any]:
    """Run two Codex profiles × three read-only instances on the sealed spec.

    ``execute`` replaces the process spawn in tests. It returns the session
    JSONL, the final message, and the CLI version. It does not supply quality,
    token counts, or a model; those are read from the session.
    """
    manifest = build_current_recurring_trial(
        orchestrator_root,
        workflows_root,
        seed=seed,
        now=now,
        capacity_state=capacity_state,
        state_dir=state_dir,
        experiments_root=experiments_root,
    )
    validate_trial_manifest(manifest)
    roots = [Path(row["root"]) for row in manifest["source_before"].values()]
    if artifact_dir is None:
        state_root = Path(
            os.environ.get("ORCH_STATE_DIR", str(Path.home() / ".codex" / "orchestrator"))
        )
        artifact_dir = (
            state_root
            / "capability-program"
            / "profile-trial-attempts"
            / manifest["trial_id"].replace(":", "-")
        )
    ensure_artifact_outside_sources(artifact_dir, roots)
    if state_path is not None:
        ensure_artifact_outside_sources(state_path, roots)
    sessions = []
    cli_versions: list[str] = []
    for request in sorted(manifest["requests"], key=lambda row: int(row["launch_ordinal"])):
        prompt = recurring_trial_prompt(manifest, request)
        if execute is None:
            binary = codex_bin or (Path(shutil.which("codex") or "codex"))
            workdir = artifact_dir / f"work-{int(request['launch_ordinal'])}"
            session_text, final_text, cli_version = execute_codex_read_only(
                binary, request, prompt, workdir
            )
        else:
            session_text, final_text, cli_version = execute(request, prompt)
        if not str(cli_version or "").strip():
            raise ValueError("codex read-only runner did not report a version")
        cli_versions.append(str(cli_version))
        sessions.append(
            {
                "run_id": request["run_id"],
                "session_text": session_text,
                "final_text": final_text,
            }
        )
    if len(set(cli_versions)) != 1:
        raise ValueError("codex arms did not report one CLI version")
    results = results_from_codex_sessions(
        manifest,
        sessions,
        artifact_dir=artifact_dir,
        cli_version=cli_versions[0],
        runner_version="codex-exec-read-only",
    )
    return publish_measured_recurring_trial(manifest, results, state_path=state_path, now=now)


def publish_measured_recurring_trial(
    manifest: dict[str, Any],
    results: dict[str, Any],
    *,
    state_path: Path | None = None,
    now: int | None = None,
    provider_response_lookup: Callable[[str], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Record one measured two-profile, three-instance trial of the sealed spec.

    This does not execute a worker. The attempts must already carry provider-resolved
    identity and a nonnegative token count in both directions. A missing count is
    refused before any feedback row or program summary is written, so an unmeasured
    arm cannot be stored as zero. Explicitly unverified measurements are recorded
    without a verified-ingest marker. A claimed verified result must pass the
    independent provider lookup before any writes.
    """
    packet = manifest.get("frozen_packet")
    if (
        not isinstance(packet, dict)
        or packet.get("work_shape") != "strategy-experiment-frozen-spec"
    ):
        raise ValueError("measured recurring trial requires the strategy experiment frozen spec")
    if list(manifest.get("selected_profile_ids") or []) != list(CURRENT_TRIAL_PROFILE_IDS):
        raise ValueError("measured recurring trial requires the two current Codex profiles")
    if manifest.get("instances_per_profile") != CURRENT_TRIAL_INSTANCES:
        raise ValueError("measured recurring trial requires three instances per profile")
    fp = manifest["frozen_packet"]
    if not isinstance(fp, dict):
        raise ValueError("measured recurring trial requires a frozen packet")
    frozen_spec = fp.get("frozen_spec")
    if not isinstance(frozen_spec, str) or not frozen_spec.strip():
        raise ValueError("measured recurring trial requires a nonempty frozen_spec")
    exp_id = fp.get("exp_id")
    if not isinstance(exp_id, str) or not _EXPERIMENT_ID_RE.fullmatch(exp_id):
        raise ValueError("measured recurring trial requires a valid strategy exp_id")
    spec_sha = fp.get("spec_sha256")
    expected_sha = "sha256:" + hashlib.sha256(frozen_spec.encode("utf-8")).hexdigest()
    if spec_sha != expected_sha:
        raise ValueError("measured recurring trial spec_sha256 does not seal frozen_spec")
    attempts = results.get("attempts")
    expected = len(CURRENT_TRIAL_PROFILE_IDS) * CURRENT_TRIAL_INSTANCES
    if not isinstance(attempts, list) or len(attempts) != expected:
        raise ValueError("measured recurring trial requires six attempts")
    for attempt in attempts:
        if not isinstance(attempt, dict):
            raise ValueError("measured recurring trial attempt is not an object")
        for field in ("tokens_in", "tokens_out"):
            value = attempt.get(field)
            if type(value) is not int or value < 0:
                raise ValueError(f"measured recurring trial {field} must be a nonnegative integer")
    return finalize_trial(
        manifest,
        results,
        state_path=state_path,
        record_feedback=True,
        ingest_brain=results.get("identity_verified") is True,
        now=now,
        provider_response_lookup=provider_response_lookup,
    )


def finalize_trial(
    manifest: dict[str, Any],
    results: dict[str, Any],
    *,
    state_path: Path | None = None,
    record_feedback: bool = False,
    ingest_brain: bool = False,
    now: int | None = None,
    provider_response_lookup: Callable[[str], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Validate source integrity, record instrumentation, and prove no learning writes."""
    validate_trial_manifest(manifest)
    attempts = _validate_results(manifest, results)
    timestamp = int(time.time()) if now is None else int(now)
    # Both gates run before the feedback transaction. A refusal leaves no rows,
    # and brain_ingest_enabled stays false on any envelope this call returns.
    _require_consistent_source_seal(manifest)
    source_after = {
        key: source_manifest(Path(value["root"]))
        for key, value in manifest["source_before"].items()
    }
    source_unchanged = all(
        source_after[key]["aggregate_sha256"] == recorded["aggregate_sha256"]
        and source_after[key]["entries"] == recorded["entries"]
        for key, recorded in manifest["source_before"].items()
    )
    if not source_unchanged:
        raise ValueError("source integrity changed during read-only trial")

    identity_verified = results.get(
        "identity_verified"
    ) is True and _authoritative_identity_verified(
        attempts, provider_response_lookup=provider_response_lookup
    )
    if ingest_brain:
        _require_verified_identity_for_ingest(results, identity_verified=identity_verified)
    quality_by_profile = _validated_quality_by_profile(results, attempts)

    write_feedback = record_feedback or ingest_brain
    weights_before = _weight_snapshot() if write_feedback else {}
    recorded_attempt_ids: list[str] = []
    brain_ingest_enabled = False
    ingest_status = None
    if write_feedback:
        # Session identity is a claim until the independent lookup confirms it.
        feedback_attempts = [dict(attempt) for attempt in attempts]
        if not identity_verified:
            for attempt in feedback_attempts:
                attempt["provider_resolved_provider"] = None
                attempt["provider_resolved_model"] = None
        ingest_payload = feedback.ingest_profile_trial(
            results,
            manifest=manifest,
            attempts=feedback_attempts,
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
            replacement_measured = any(
                attempt.get(field) is not None
                for attempt in attempts
                for field in ("tokens_in", "tokens_out")
            )
            if not ingest_brain and same_trial_measured and not replacement_measured:
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

    if ingest_status == "already_ingested":
        # A retry wrote no rows; do not replace the committed trial state.
        if state_path:
            committed = build_report(state_path)
            if committed.get("trial_id") == manifest["trial_id"]:
                return {key: value for key, value in committed.items() if key != "path"}
        identity_verified = True
        brain_ingest_enabled = True

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
        "identity_verified": identity_verified,
        "next_action": "retain_shadow_and_collect_only_productive_outcomes",
    }
    if state_path and ingest_status != "already_ingested":
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

        forged = json.loads(_canonical(prepared))
        (workflows_root / "worker.yml").write_text(
            "worker: mutated-during-trial\n", encoding="utf-8"
        )
        live_workflows = source_manifest(workflows_root)
        forged["source_before"]["workflows"]["aggregate_sha256"] = live_workflows[
            "aggregate_sha256"
        ]
        old_trial_id = forged["trial_id"]
        forged["trial_id"] = _expected_trial_id(forged)
        for request in forged["requests"]:
            request["run_id"] = forged["trial_id"] + request["run_id"][len(old_trial_id) :]
        forged_results = json.loads(_canonical(results))
        forged_results["trial_id"] = forged["trial_id"]
        for attempt in forged_results["attempts"]:
            attempt["run_id"] = forged["trial_id"] + attempt["run_id"][len(old_trial_id) :]
        previous_db = feedback.DB_PATH
        feedback.DB_PATH = root / "quarantine-selftest.db"
        try:
            try:
                finalize_trial(
                    forged,
                    forged_results,
                    record_feedback=True,
                    ingest_brain=True,
                    now=2_001,
                )
            except ValueError as exc:
                assert "aggregate does not match its entries" in str(
                    exc
                ), f"forged source seal was not refused: {exc}"
            else:
                raise AssertionError("forged source seal was ingested")
        finally:
            feedback.DB_PATH = previous_db

        state_dir = root / "recurring-state"
        experiments = root / "experiments"
        try:
            resolve_strategy_frozen_packet(state_dir=state_dir, experiments_root=experiments)
        except RecurringWorkUnavailable as exc:
            assert "strategy experiment receipt is absent" in str(exc), exc
        else:
            raise AssertionError("a missing strategy experiment spec was accepted")
        spec_text = "Acceptance criteria: the frozen recurring-work body.\n"
        exp_id = "strategy434-20261006"
        experiment = experiments / exp_id
        experiment.mkdir(parents=True)
        (experiment / "spec.md").write_text(spec_text, encoding="utf-8")
        (experiment / "strategy.json").write_text(
            json.dumps({"exp_id": exp_id, "spec_file": str(experiment / "spec.md")}),
            encoding="utf-8",
        )
        (experiment / "meta.json").write_text(
            json.dumps({"spec_sha256": hashlib.sha256(spec_text.encode("utf-8")).hexdigest()}),
            encoding="utf-8",
        )
        receipt_path = state_dir / "capability-program" / "strategy-experiment.json"
        receipt_path.parent.mkdir(parents=True)
        receipt_path.write_text(json.dumps({"exp_id": exp_id, "status": "completed"}))
        recurring = build_current_recurring_trial(
            orchestrator_root,
            workflows_root,
            seed=14,
            now=1_000,
            capacity_state=capacity.OK,
            state_dir=state_dir,
            experiments_root=experiments,
        )
        validate_trial_manifest(recurring)
        assert recurring["frozen_packet"]["frozen_spec"] == spec_text
        assert recurring["frozen_packet"]["exp_id"] == exp_id
        assert recurring["selected_profile_ids"] == list(CURRENT_TRIAL_PROFILE_IDS)
        assert recurring["instances_per_profile"] == CURRENT_TRIAL_INSTANCES
        assert len(recurring["requests"]) == 6
        for profile_id in CURRENT_TRIAL_PROFILE_IDS:
            ordinals = sorted(
                row["instance_ordinal"]
                for row in recurring["requests"]
                if row["profile_id"] == profile_id
            )
            assert ordinals == [1, 2, 3], ordinals
        assert not (state_dir / "capability-program" / "profile-trial.json").exists()
        saved_state = os.environ.get("ORCH_STATE_DIR")
        os.environ["ORCH_STATE_DIR"] = str(state_dir)
        try:
            _write_capability_program_trial_summary(
                recurring,
                [
                    {
                        "profile_id": profile_id,
                        "provider_resolved_provider": "openai",
                        "provider_resolved_model": execution_profiles.get_profile(profile_id)[
                            "requested_model"
                        ],
                        "tokens_in": 3,
                        "tokens_out": 1,
                    }
                    for profile_id in CURRENT_TRIAL_PROFILE_IDS
                    for _ in range(CURRENT_TRIAL_INSTANCES)
                ],
                identity_verified=True,
                brain_ingest_enabled=False,
                quality_by_profile={profile_id: 1.0 for profile_id in CURRENT_TRIAL_PROFILE_IDS},
                timestamp=1_001,
            )
            published = json.loads(
                (state_dir / "capability-program" / "profile-trial.json").read_text(
                    encoding="utf-8"
                )
            )
        finally:
            if saved_state is None:
                os.environ.pop("ORCH_STATE_DIR", None)
            else:
                os.environ["ORCH_STATE_DIR"] = saved_state
        assert published["exp_id"] == exp_id, published
        assert published["work_shape"] == "strategy-experiment-frozen-spec", published
        assert published["spec_sha256"] == recurring["frozen_packet"]["spec_sha256"], published
        assert (published["profile_count"], published["instance_count"]) == (2, 6), published
        assert set(published["provider_resolved_identity_by_profile"]) == set(
            CURRENT_TRIAL_PROFILE_IDS
        )
        stub = "[SPEC_PROVENANCE: MISSING_SPEC_STUB]\n"
        (experiment / "spec.md").write_text(stub, encoding="utf-8")
        (experiment / "meta.json").write_text(
            json.dumps({"spec_sha256": hashlib.sha256(stub.encode("utf-8")).hexdigest()}),
            encoding="utf-8",
        )
        try:
            resolve_strategy_frozen_packet(state_dir=state_dir, experiments_root=experiments)
        except RecurringWorkUnavailable as exc:
            assert "missing-spec stub" in str(exc), exc
        else:
            raise AssertionError("a stub strategy spec was accepted")

        # DELIBERATE BREAK: treating the CLI's turn_context model, or a generic
        # model field on turn.completed, as provider identity would make these
        # assertions fail. Revert by reading that field inside _provider_witness.
        try:
            _provider_witness([{"type": "turn_context", "payload": {"model": "gpt-6-astra"}}])
        except ValueError as exc:
            assert "provider_resolved_identity_unavailable" in str(exc), exc
        else:
            raise AssertionError("CLI turn_context was accepted as provider identity")
        try:
            _provider_witness(
                [
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                        "model": "gpt-6-astra",
                    }
                ]
            )
        except ValueError as exc:
            assert "provider_resolved_identity_unavailable" in str(exc), exc
        else:
            raise AssertionError("turn.completed model was accepted as provider identity")
        witnessed = _provider_witness(
            [
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 3, "output_tokens": 1},
                    "server_model": "gpt-6-astra",
                }
            ]
        )
        assert witnessed == {
            "model": "gpt-6-astra",
            "id": None,
            "usage": None,
            "source": "server_model",
        }, witnessed
        parsed = _one_provider_response(
            [
                {
                    "type": "response.completed",
                    "response": {
                        "object": "response",
                        "id": "resp_selftest",
                        "model": "gpt-6-astra",
                    },
                }
            ]
        )
        assert parsed["model"] == "gpt-6-astra", parsed
        assert parsed["id"] == "resp_selftest", parsed

    print(
        "model_profile_trial.py selftest: OK "
        "(prepare/source proof, accept/reject validation, shadow report/no promotion, "
        "forged source seal refuses ingest, frozen spec binds the two-by-three plan "
        "and its published summary, CLI turn_context and turn.completed model are "
        "not provider identity; session model claims need independent provenance)"
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
    prepare.add_argument("--profile-id", action="append", help="repeat for each selected profile")
    prepare.add_argument("--instances-per-profile", type=int, default=1)
    prepare.add_argument("--packet", type=Path, help="frozen recurring-work packet as JSON")
    prepare.add_argument(
        "--capacity-state", choices=("ok", "warn", "shed", "unknown"), default="unknown"
    )
    current = sub.add_parser("prepare-current")
    current.add_argument("--orchestrator-root", type=Path, required=True)
    current.add_argument("--workflows-root", type=Path, required=True)
    current.add_argument("--output", type=Path, required=True)
    current.add_argument("--seed", type=int, default=56014)
    current.add_argument(
        "--capacity-state", choices=("ok", "warn", "shed", "unknown"), default="unknown"
    )
    current.add_argument("--state-dir", type=Path)
    current.add_argument("--experiments-root", type=Path)
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
    publish = sub.add_parser("publish-current")
    publish.add_argument("--manifest", type=Path, required=True)
    publish.add_argument("--results", type=Path, required=True)
    publish.add_argument("--state", type=Path, required=True)
    run_current = sub.add_parser("run-current")
    run_current.add_argument("--orchestrator-root", type=Path, required=True)
    run_current.add_argument("--workflows-root", type=Path, required=True)
    run_current.add_argument("--state", type=Path, required=True)
    run_current.add_argument("--seed", type=int, default=56014)
    run_current.add_argument(
        "--capacity-state", choices=("ok", "warn", "shed", "unknown"), default="unknown"
    )
    run_current.add_argument("--state-dir", type=Path)
    run_current.add_argument("--experiments-root", type=Path)
    run_current.add_argument("--artifact-dir", type=Path)
    run_current.add_argument("--codex-bin", type=Path)
    report = sub.add_parser("report")
    report.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    args = parser.parse_args(argv)

    if args.selftest:
        _selftest()
        return 0
    if args.command is None:
        parser.error(
            "one of --selftest, prepare, prepare-current, run-current, publish-current, "
            "finalize, or report is required"
        )

    _capability_heartbeat()
    if args.command == "prepare":
        ensure_artifact_outside_sources(args.output, (args.orchestrator_root, args.workflows_root))
        payload = build_trial_manifest(
            args.orchestrator_root,
            args.workflows_root,
            seed=args.seed,
            capacity_state=args.capacity_state,
            profile_ids=args.profile_id,
            instances_per_profile=args.instances_per_profile,
            packet=json.loads(args.packet.read_text(encoding="utf-8")) if args.packet else None,
        )
        _atomic_json(args.output, payload)
    elif args.command == "prepare-current":
        ensure_artifact_outside_sources(args.output, (args.orchestrator_root, args.workflows_root))
        try:
            payload = build_current_recurring_trial(
                args.orchestrator_root,
                args.workflows_root,
                seed=args.seed,
                capacity_state=args.capacity_state,
                state_dir=args.state_dir,
                experiments_root=args.experiments_root,
            )
        except RecurringWorkUnavailable as exc:
            print(f"model_profile_trial.py: {exc}", file=sys.stderr)
            return 2
        _atomic_json(args.output, payload)
    elif args.command == "publish-current":
        try:
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            loaded_results = json.loads(args.results.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict) or not isinstance(loaded_results, dict):
                raise ValueError("measured recurring trial inputs must be objects")
            source_before = manifest.get("source_before")
            if not isinstance(source_before, dict):
                raise ValueError("measured recurring trial manifest has no source seal")
            ensure_artifact_outside_sources(
                args.state,
                [
                    Path(row["root"])
                    for row in source_before.values()
                    if isinstance(row, dict) and row.get("root")
                ],
            )
            payload = publish_measured_recurring_trial(
                manifest, loaded_results, state_path=args.state
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, KeyError) as exc:
            print(f"model_profile_trial.py: {exc}", file=sys.stderr)
            return 2
    elif args.command == "run-current":
        try:
            payload = run_current_recurring_trial(
                args.orchestrator_root,
                args.workflows_root,
                seed=args.seed,
                capacity_state=args.capacity_state,
                state_dir=args.state_dir,
                experiments_root=args.experiments_root,
                state_path=args.state,
                artifact_dir=args.artifact_dir,
                codex_bin=args.codex_bin,
            )
        except (
            RecurringWorkUnavailable,
            OSError,
            UnicodeError,
            ValueError,
            KeyError,
            subprocess.TimeoutExpired,
        ) as exc:
            print(f"model_profile_trial.py: {exc}", file=sys.stderr)
            return 2
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
