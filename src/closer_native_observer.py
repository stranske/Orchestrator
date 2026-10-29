"""Pinned native-wrapper handoff observation, never Desktop scheduler attestation.

The reviewed wrapper and this fixed observer are trusted same-UID host code. A
signature detects substituted JSON; it is not an OS boundary against that user.
No provider, queue or scheduler-control API is used by qualification.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import inspect
import json
import math
import os
import re
import secrets
import shlex
import signal
import stat
import subprocess
import time
from pathlib import Path

import closer_mechanisms as mechanisms

POLICY = "native-configured-policy/v1"
OBSERVATION = "native-wrapper-lifecycle/v1"
REQUEST = "native-wrapper-qualification-request/v1"
MARKER = "# BEGIN CLOSER PINNED NATIVE ENTRY v1"
END_MARKER = "# END CLOSER PINNED NATIVE ENTRY v1"
_CURRENT_ENTRY: dict[str, str] = {}
_CURRENT_ADMISSION: dict[str, dict] = {}


def _hash(path):
    if Path(path).is_symlink():
        raise mechanisms.MechanismError("native pinned artifact is symlinked")
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _bounded_bytes(path, limit):
    path = Path(path)
    if path.resolve() != path or path.is_symlink():
        raise mechanisms.MechanismError("native bounded input is aliased")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise mechanisms.MechanismError("native bounded input exceeds regular-file limit")
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        current = path.stat(follow_symlinks=False)
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if (
        len(raw) > limit
        or len(raw) != before.st_size
        or not stat.S_ISREG(current.st_mode)
        or any(getattr(before, key) != getattr(after, key) for key in fields)
        or any(getattr(after, key) != getattr(current, key) for key in fields)
    ):
        raise mechanisms.MechanismError("native bounded input changed during read")
    return raw


def policy_problem(policy):
    if policy.get("schema") != POLICY or policy.get("mechanism") != mechanisms.NATIVE:
        return "native configured policy contract missing"
    # Same canonical supervising lane, not a provider-inferred automation name.
    mapped = {**policy, "schema": mechanisms.POLICY, "mechanism": mechanisms.CLI}
    problem = mechanisms.configured_policy_problem(mapped)
    return problem.replace("CLI configured", "native configured") if problem else None


PRIVATE_TEST = {
    "schema": "closer-private-native-test-wrapper/v1",
    "purpose": "codex-account-metadata-only",
}


def private_test_wrapper_text(manifest_path, campaign_root, interpreter, launcher):
    """Fixed no-fallback test wrapper using the existing native entry producer."""
    return (
        '#!/bin/bash\nset -euo pipefail\nLANE="${1:?}"\n'
        'case "$LANE" in closer) ;; *) exit 78 ;; esac\n'
        + wrapper_block(manifest_path, campaign_root, interpreter, launcher)
        + "exit 78\n"
    )


def _private_test_wrapper(manifest, value):
    """Prospective metadata-only test source, never an installed runner alias."""
    selection = value.get("private_test")
    if selection not in (PRIVATE_TEST, {**PRIVATE_TEST, "purpose": "codex-public-coding-test"}):
        raise mechanisms.MechanismError("unsupported private native test purpose")
    if selection["purpose"] == "codex-public-coding-test":
        task = (manifest.get("policy") or {}).get("codex_public_coding_test")
        if type(task) is not dict or task.get("schema") != "closer-public-coding-canary/v1":
            raise mechanisms.MechanismError("private public coding task is not source selected")
    policy = manifest.get("policy") or {}
    if (
        type(policy.get("codex_metadata_qualification")) is not dict
        or type(policy.get("codex_initial_consumer")) is not dict
        or any(
            key in policy
            for key in (
                "correction_controller_installation",
                "native_correction_cleanup",
                "coding_continuation",
            )
        )
    ):
        raise mechanisms.MechanismError("private native test is metadata-only")
    capacity = value.get("capacity_publication") or {}
    if capacity.get("profile_ids") != [] or capacity.get("bounded_trial_agents") != []:
        raise mechanisms.MechanismError("private native test cannot grant model capacity")
    root = Path(value.get("observation_root", ""))
    if (
        not root.is_absolute()
        or root.resolve() != root
        or any(parent.is_symlink() for parent in (root, *root.parents))
    ):
        raise mechanisms.MechanismError("private native test root is not canonical")
    meta = root.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(meta.st_mode)
        or meta.st_uid != os.getuid()
        or stat.S_IMODE(meta.st_mode) != 0o700
    ):
        raise mechanisms.MechanismError("private native test root is not owned/private")
    path = root / "private-test-wrapper.sh"
    pin = value.get("wrapper") or {}
    if pin.get("path") != str(path) or path.resolve() != path or path.is_symlink():
        raise mechanisms.MechanismError("private native test wrapper path differs")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or before.st_mode & 0o222
            or not before.st_mode & 0o100
            or not 0 < before.st_size <= 16384
        ):
            raise mechanisms.MechanismError("private native test wrapper mode differs")
        raw = stream.read(16385)
        after = os.fstat(stream.fileno())
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        current = path.stat(follow_symlinks=False)
        if (
            len(raw) != before.st_size
            or any(getattr(before, k) != getattr(after, k) for k in fields)
            or any(getattr(after, k) != getattr(current, k) for k in fields)
            or hashlib.sha256(raw).hexdigest() != pin.get("sha256")
        ):
            raise mechanisms.MechanismError("private native test wrapper changed")
    command = manifest.get("command")
    if (
        type(command) is not list
        or command.count("--manifest") != 1
        or command.index("--manifest") + 1 >= len(command)
    ):
        raise mechanisms.MechanismError("private native test manifest path is unbound")
    manifest_path = command[command.index("--manifest") + 1]
    if type(manifest_path) is not str or str(Path(manifest_path).resolve()) != manifest_path:
        raise mechanisms.MechanismError("private native test manifest path is not canonical")
    expected = private_test_wrapper_text(
        manifest_path,
        value.get("campaign_root"),
        value.get("interpreter", {}).get("path"),
        manifest.get("launcher", {}).get("path"),
    ).encode("utf-8")
    if raw != expected:
        raise mechanisms.MechanismError("private native test wrapper source differs")
    return path


def private_test_assignment_source(manifest):
    """Only the fixed owned private TEST assignment; ordinary discovery unchanged."""
    value = manifest.get("mechanism_contract") or {}
    if "private_test" not in value:
        if "private_test_assignment_sha256" in value:
            raise mechanisms.MechanismError("private assignment requires explicit test purpose")
        return (
            Path.home()
            / ".codex/automations/reviewed-repo-automation-controller/closer-assignment.json"
        )
    _private_test_wrapper(manifest, value)
    root = Path(value["observation_root"])
    path = root / "private-test-assignment.json"
    raw = _bounded_bytes(path, 65536)
    meta = path.stat(follow_symlinks=False)
    if (
        meta.st_uid != os.getuid()
        or meta.st_nlink != 1
        or stat.S_IMODE(meta.st_mode) != 0o600
        or hashlib.sha256(raw).hexdigest() != value.get("private_test_assignment_sha256")
    ):
        raise mechanisms.MechanismError("private native test assignment pin/mode differs")
    from closer_model_contracts import strict_response_json

    doc = strict_response_json(raw.decode("utf-8"))
    purpose = value["private_test"]["purpose"]
    targets = [manifest["policy"]["codex_initial_consumer"]["target"]]
    if purpose == "codex-public-coding-test":
        targets.append(manifest["policy"]["codex_public_coding_test"]["target"])
    expected = {
        "schema": "closer-private-native-test-assignment/v1",
        "purpose": purpose,
        "targets": [{"target": target} for target in targets],
    }
    if doc != expected or len(set(targets)) != len(targets):
        raise mechanisms.MechanismError("private native test assignment scope differs")
    return path


def contract(manifest, *, _scope=None):
    value = manifest.get("mechanism_contract") or {}
    if value.get("schema") != mechanisms.CONTRACT or value.get("mechanism") != mechanisms.NATIVE:
        raise mechanisms.MechanismError("native mechanism contract missing")
    native_digests = None
    if _scope is not None:
        import closer_source_installation as source

        native_digests = source._scoped_native_runtime_digests(_scope, manifest)
    # Concrete supported wrapper. Other actors remain independent capabilities.
    wrapper = (
        _private_test_wrapper(manifest, value)
        if "private_test" in value
        else Path.home() / ".codex/bin/cursor-lane.sh"
    )
    pin = value.get("wrapper") or {}
    if value.get("actor") != "cursor" or pin.get("path") != str(wrapper):
        raise mechanisms.MechanismError("native wrapper/actor is not supported")
    if (
        wrapper.is_symlink()
        or wrapper.resolve() != wrapper
        or (_hash(wrapper) if native_digests is None else native_digests[str(wrapper)])
        != pin.get("sha256")
    ):
        raise mechanisms.MechanismError("native wrapper pin changed or aliased")
    interpreter = Path(value.get("interpreter", {}).get("path", ""))
    if (
        interpreter.resolve() != interpreter
        or not interpreter.is_file()
        or (_hash(interpreter) if native_digests is None else native_digests[str(interpreter)])
        != value.get("interpreter", {}).get("sha256")
        or Path(manifest["runtime"]["python"]).resolve() != interpreter
    ):
        raise mechanisms.MechanismError("native physical interpreter pin differs")
    root = Path(value.get("observation_root", ""))
    if (
        not root.is_absolute()
        or root.resolve() != root
        or not root.is_dir()
        or root.stat().st_mode & 0o777 != 0o700
    ):
        raise mechanisms.MechanismError("native observation root must be canonical/private")
    campaign_root = Path(value.get("campaign_root", ""))
    if not campaign_root.is_absolute() or campaign_root.resolve() != campaign_root:
        raise mechanisms.MechanismError("native campaign root is not canonical")
    if "closer_native_observer.py" not in (manifest.get("modules") or {}):
        raise mechanisms.MechanismError("native observer is outside pinned package")
    problem = policy_problem(manifest.get("policy") or {})
    if problem:
        raise mechanisms.MechanismError(problem)
    capacity_contract(manifest, control_plane_only=True)
    return value, wrapper, interpreter, root


def capacity_contract(manifest, *, control_plane_only=False):
    """Explicit host opt-in; never infer quota or profile scope from the wrapper actor."""
    import closer_role_qualification as roles
    import execution_profiles
    from closer_agent_launchers import APPROVED_AGENTS

    value = (manifest.get("mechanism_contract") or {}).get("capacity_publication")
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("schema") not in {
        "native-scoped-capacity/v1",
        "native-scoped-capacity/v2",
    }:
        raise mechanisms.MechanismError("native capacity publication contract missing")
    if value["schema"] == "native-scoped-capacity/v2":
        from closer_frontier_policy import POLICY, request_window

        if value.get("execution_policy") != POLICY or "admission_seconds" in value:
            raise mechanisms.MechanismError("native finite frontier policy must be explicit")
        try:
            request_window(0, value.get("request_timeout_seconds"))
            if value["request_timeout_seconds"] > 120:
                raise ValueError("native request timeout exceeds supported 120 seconds")
        except (ValueError, TypeError) as exc:
            raise mechanisms.MechanismError("native finite request timeout invalid") from exc
        seconds = value.get("freshness_seconds")
    else:
        if any(
            key in value
            for key in ("execution_policy", "request_timeout_seconds", "freshness_seconds")
        ):
            raise mechanisms.MechanismError(
                "legacy native capacity cannot infer finite frontier policy"
            )
        seconds = value.get("admission_seconds")
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, (int, float))
        or not math.isfinite(seconds)
        or not 1 <= seconds <= 900
    ):
        raise mechanisms.MechanismError(
            "native admission/freshness window must be bounded by 900 seconds"
        )
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", str(value.get("owner", ""))):
        raise mechanisms.MechanismError("native capacity publication owner missing")
    if type(value.get("continue_on_assignment_change", False)) is not bool:
        raise mechanisms.MechanismError("native standing continuation flag must be boolean")
    profiles, trials = value.get("profile_ids"), value.get("bounded_trial_agents")
    if (
        not isinstance(profiles, list)
        or (not profiles and not control_plane_only)
        or any(not isinstance(p, str) for p in profiles)
        or len(set(profiles)) != len(profiles)
        or not isinstance(trials, list)
        or any(not isinstance(a, str) or a not in APPROVED_AGENTS for a in trials)
    ):
        raise mechanisms.MechanismError("native capacity exact profile/trial scope missing")
    for key in profiles:
        if not key or len(key) > 160 or not re.fullmatch(r"[A-Za-z0-9_.-]+", key):
            raise mechanisms.MechanismError("native capacity profile identity is malformed")
        if control_plane_only:
            continue
        profile = execution_profiles.get_profile(key)
        if (
            not profile
            or profile.get("agent") not in APPROVED_AGENTS
            or profile.get("lifecycle_status") != "active"
            or "offload" not in profile.get("transport_support", [])
            or not any(roles.is_runtime_qualified(profile, role) for role in roles.ROLES)
        ):
            raise mechanisms.MechanismError(
                "native capacity profile is not approved active qualified offload"
            )
    return value


def _immutable(path, raw):
    """Create once and fsync; existing bytes are never replaced on restart."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _demand_cohort(raw, finite_frontier):
    assignment = json.loads(raw)
    return {
        "assignment_sha256": hashlib.sha256(raw).hexdigest(),
        "targets_sha256": mechanisms.digest(assignment["targets"]),
        "finite_frontier_sha256": mechanisms.digest(finite_frontier),
    }


def _validate_demand_continuations(manifest, origin, campaign, raw, refs, finite_frontier):
    """Only an explicit contiguous signed execution-generation chain may advance."""
    if not isinstance(refs, list) or len(refs) > 64:
        raise mechanisms.MechanismError("demand continuation chain exceeds bound")
    generation = origin["campaign_generation"]
    if type(generation) is not int or generation < 1 or type(campaign.get("generation")) is not int:
        raise mechanisms.MechanismError("demand execution generation malformed")
    if not refs:
        if generation != campaign["generation"]:
            raise mechanisms.MechanismError("demand execution lacks signed continuation")
        return
    root = contract(manifest)[3]
    expected_binding = binding(manifest)
    key = mechanisms._key(root)
    expected_campaign = {name: campaign[name] for name in ("state", "campaign_id", "owner")}
    cohort = _demand_cohort(raw, finite_frontier)
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
            raise mechanisms.MechanismError("demand continuation reference malformed")
        path = Path(ref["path"])
        if (
            path.parent.parent != root
            or not re.fullmatch(r"[0-9a-f]{32}", path.parent.name)
            or not re.fullmatch(r"demand-continuation-[0-9a-f]{64}\.json", path.name)
        ):
            raise mechanisms.MechanismError("demand continuation escapes native observation root")
        encoded = _bounded_bytes(path, 128 * 1024)
        signed = json.loads(encoded)
        record = signed["record"]
        if (
            hashlib.sha256(encoded).hexdigest() != ref["sha256"]
            or record.get("schema") != "native-demand-continuation/v1"
            or record.get("origin") != origin
            or record.get("cohort") != cohort
            or record.get("campaign") != expected_campaign
            or record.get("release_pin") != manifest["package_digest"]
            or record.get("binding") != expected_binding
            or record.get("entry_nonce") != path.parent.name
            or type(record.get("previous_generation")) is not int
            or type(record.get("next_generation")) is not int
            or record["previous_generation"] != generation
            or record["next_generation"] != generation + 1
            or not isinstance(record.get("releases"), list)
            or not record["releases"]
            or not hmac.compare_digest(
                str(signed.get("signature", "")), mechanisms._signature(record, key)
            )
        ):
            raise mechanisms.MechanismError(
                "demand continuation signature/scope/generation differs"
            )
        generation = record["next_generation"]
    if generation != campaign["generation"]:
        raise mechanisms.MechanismError("demand continuation chain is incomplete")


def seal_demand_hold_continuation(state_path, doc, next_generation, releases):
    """Called only inside the existing validated hold-rearm Campaign transaction."""
    import copy

    from closer_campaign import load_manifest

    manifest = load_manifest(doc["worker_config"]["runtime_manifest"])
    if not entry_active(manifest):
        raise mechanisms.MechanismError("demand continuation requires actual observed native entry")
    previous = doc["generation"]
    if (
        type(previous) is not int
        or type(next_generation) is not int
        or next_generation != previous + 1
        or not releases
        or doc.get("release_pin") != manifest["package_digest"]
    ):
        raise mechanisms.MechanismError("demand hold transition identity differs")
    for released in releases:
        held = doc["hold_followups"][released["hold_id"]]
        if held.get("status") != "released" or time.time() >= held["deadline"]:
            raise mechanisms.MechanismError("demand hold release expired or changed before seal")
    identity = _campaign_identity(state_path, doc)
    origin = doc["demand_assignment"]
    admission = doc["demand_admission"]
    from closer_demand_admission import RECORD_SCHEMA
    from closer_demand_admission import _hash as admission_hash

    if (
        admission.get("schema") != RECORD_SCHEMA
        or admission.get("record_digest")
        != admission_hash({k: v for k, v in admission.items() if k != "record_digest"})
        or admission.get("package_digest") != manifest["package_digest"]
        or doc["worker_config"].get("assignment") != origin.get("path")
        or origin.get("sha256") != admission.get("assignment_sha256")
        or any(
            origin.get(name) != admission.get(name)
            for name in (
                "campaign_id",
                "owner",
                "campaign_generation",
                "assignment_generation",
                "request_id",
            )
        )
    ):
        raise mechanisms.MechanismError("demand continuation original admission/snapshot changed")
    _source, raw = _demand_assignment_bytes(
        manifest,
        origin,
        identity,
        continuations=doc.get("demand_continuations", []),
        finite_frontier=doc["worker_config"].get("finite_frontier"),
    )
    root = contract(manifest)[3]
    nonce = _CURRENT_ENTRY[str(root)]
    record = {
        "schema": "native-demand-continuation/v1",
        "origin": copy.deepcopy(origin),
        "cohort": _demand_cohort(raw, doc["worker_config"].get("finite_frontier")),
        "campaign": {name: identity[name] for name in ("state", "campaign_id", "owner")},
        "release_pin": manifest["package_digest"],
        "binding": binding(manifest),
        "previous_generation": previous,
        "next_generation": next_generation,
        "releases": copy.deepcopy(releases),
        "entry_nonce": nonce,
    }
    path = root / nonce / ("demand-continuation-" + mechanisms.digest(record) + ".json")
    encoded = json.dumps(
        {"record": record, "signature": mechanisms._signature(record, mechanisms._key(root))},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    if len(encoded) > 128 * 1024:
        raise mechanisms.MechanismError("demand continuation receipt exceeds bound")
    try:
        _immutable(path, encoded)
    except FileExistsError:
        if _bounded_bytes(path, 128 * 1024) != encoded:
            raise mechanisms.MechanismError("demand continuation immutable bytes changed")
    return {"path": str(path), "sha256": hashlib.sha256(encoded).hexdigest()}


def _demand_assignment_bytes(
    manifest, reference, campaign, *, continuations=None, finite_frontier=None
):
    """Exact admitted snapshot origin, independently bound to the pinned state."""
    contract = manifest["mechanism_contract"].get("controller_demand") or {}
    keys = {
        "schema",
        "path",
        "sha256",
        "campaign_id",
        "owner",
        "campaign_generation",
        "assignment_generation",
        "request_id",
    }
    if (
        contract.get("schema") != "closer-controller-demand/v1"
        or not isinstance(reference, dict)
        or set(reference) != keys
        or reference.get("schema") != "closer-demand-assignment/v1"
        or campaign.get("state") != contract.get("campaign_state")
        or campaign.get("campaign_id") != contract.get("campaign_id")
        or campaign.get("owner") != contract.get("owner")
        or any(reference.get(key) != campaign.get(key) for key in ("campaign_id", "owner"))
        or type(reference.get("assignment_generation")) is not int
        or reference["assignment_generation"] <= 0
        or not re.fullmatch(r"[0-9a-f]{64}", str(reference.get("sha256", "")))
        or not re.fullmatch(r"[0-9a-f]{64}", str(reference.get("request_id", "")))
    ):
        raise mechanisms.MechanismError("typed demand snapshot identity differs")
    state = Path(contract["campaign_state"])
    path = state.with_name(state.name + ".demand") / (reference["sha256"] + ".json")
    if (
        state.parent != Path(manifest["mechanism_contract"]["campaign_root"])
        or state.resolve() != state
        or str(path) != reference["path"]
    ):
        raise mechanisms.MechanismError("typed demand snapshot escapes canonical campaign")
    raw = _bounded_bytes(path, 2 * 1024 * 1024)
    assignment = json.loads(raw)
    if (
        hashlib.sha256(raw).hexdigest() != reference["sha256"]
        or type(assignment.get("generation")) is not int
        or assignment["generation"] != reference["assignment_generation"]
    ):
        raise mechanisms.MechanismError("typed demand snapshot bytes/generation differ")
    _validate_demand_continuations(
        manifest,
        reference,
        campaign,
        raw,
        [] if continuations is None else continuations,
        finite_frontier,
    )
    return path, raw


def publish_admission(manifest, manifest_path, state_path, doc):
    """Publish only for a genuinely pending new round; never renew an old worker."""
    import closer_operational_capacity as scoped

    policy = capacity_contract(manifest, control_plane_only=True)
    control_plane_only = False
    control_plane_reasons = []
    if policy is not None:
        try:
            capacity_contract(manifest)
        except mechanisms.MechanismError as exc:
            control_plane_reasons.append(f"model qualification unavailable: {exc}")
            # Explicit zero-grant qualification; never manufacture an eligible profile.
            control_plane_only = True
    if policy is None:
        return None
    if not entry_active(manifest):
        raise mechanisms.MechanismError("native publication requires observed entry")
    cfg = doc.get("worker_config") or {}
    state_path = Path(state_path)
    if (
        state_path.resolve() != state_path
        or state_path.is_symlink()
        or state_path.parent != Path(manifest["mechanism_contract"]["campaign_root"])
        or not state_path.name.endswith(".campaign.json")
    ):
        raise mechanisms.MechanismError("native publication campaign path is outside pinned root")
    if (
        doc.get("hold")
        or doc.get("phase") != "work"
        or cfg.get("granularity", "round") != "round"
        or cfg.get("coordinator_mode") != "resource_scoped"
        or cfg.get("apply") is not True
        or doc.get("targets") != {"*round*": "pending"}
        or any(
            w.get("state") != "terminal"
            or w.get("outcome") not in {"ok", "unavailable", "scoped_continuation"}
            for w in doc.get("workers", [])
        )
    ):
        raise mechanisms.MechanismError("native publication requires a new pending scoped round")
    root = contract(manifest)[3]
    invocation = _CURRENT_ADMISSION.get(str(root))
    nonce = _CURRENT_ENTRY[str(root)]
    if not invocation or invocation.get("nonce") != nonce:
        raise mechanisms.MechanismError("native original invocation admission is unavailable")
    if invocation.get("context") is not None:
        context = invocation["context"]
        if context["campaign"] != _campaign_identity(state_path, doc):
            raise mechanisms.MechanismError("native invocation already bound to another campaign")
        validate_admission(
            manifest,
            context,
            for_effects=True,
            for_test="private_test" in manifest["mechanism_contract"],
        )
        return context
    directory = root / nonce
    demand = doc.get("demand_admission")
    if manifest["mechanism_contract"].get("controller_demand") and demand is None:
        raise mechanisms.MechanismError(
            "controlled native campaign requires admitted source demand"
        )
    assignment_source = private_test_assignment_source(manifest)
    if "private_test" in manifest["mechanism_contract"] and demand is not None:
        raise mechanisms.MechanismError("private native test cannot use production demand")
    demand_reference = None
    if demand is not None:
        from closer_demand_admission import RECORD_SCHEMA
        from closer_demand_admission import _hash as demand_hash

        demand_reference = doc.get("demand_assignment")
        if (
            not isinstance(demand, dict)
            or demand.get("schema") != RECORD_SCHEMA
            or demand.get("record_digest")
            != demand_hash({k: v for k, v in demand.items() if k != "record_digest"})
            or demand.get("package_digest") != manifest["package_digest"]
            or demand.get("manifest_sha256") != _hash(Path(manifest_path))
            or demand.get("qualification_digest") != _hash(root / "native-qualification.json")
            or not isinstance(demand_reference, dict)
            or any(
                demand_reference.get(key) != demand.get(key)
                for key in (
                    "campaign_id",
                    "owner",
                    "campaign_generation",
                    "assignment_generation",
                    "request_id",
                )
            )
            or demand_reference.get("sha256") != demand.get("assignment_sha256")
        ):
            raise mechanisms.MechanismError(
                "native demand admission differs from qualified package/snapshot"
            )
        from closer_campaign import current_cohort_workers

        if (
            not current_cohort_workers(state_path, doc, manifest)
            and not demand["not_before"] <= time.time() < demand["deadline"]
        ):
            raise mechanisms.MechanismError(
                "native demand admission is not due or expired before start"
            )
        assignment_source, raw = _demand_assignment_bytes(
            manifest,
            demand_reference,
            _campaign_identity(state_path, doc),
            continuations=doc.get("demand_continuations", []),
            finite_frontier=cfg.get("finite_frontier"),
        )
    else:
        if assignment_source.resolve() != assignment_source or assignment_source.is_symlink():
            raise mechanisms.MechanismError("native canonical assignment is aliased")
        raw = _bounded_bytes(assignment_source, 2 * 1024 * 1024)
    if not raw:
        raise mechanisms.MechanismError("native assignment missing or exceeds bound")
    assignment = directory / "assignment.json"
    _immutable(assignment, raw)
    from closer_dispatcher import load_workspace_config, targets_from_assignment

    targets = targets_from_assignment(assignment)
    if not targets:
        raise mechanisms.MechanismError("native canonical assignment has no eligible targets")
    workspace_config = None
    if cfg.get("workspaces") is not None:
        pin = cfg["workspaces"]
        if (
            not isinstance(pin, dict)
            or set(pin) != {"path", "sha256"}
            or not isinstance(pin["path"], str)
            or not isinstance(pin["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", pin["sha256"])
        ):
            raise mechanisms.MechanismError("native workspace configuration pin malformed")
        source = Path(pin["path"])
        if not source.is_absolute() or source.resolve() != source:
            raise mechanisms.MechanismError("native workspace configuration is aliased")
        workspace_bytes = _bounded_bytes(source, 1024 * 1024)
        if hashlib.sha256(workspace_bytes).hexdigest() != pin["sha256"]:
            raise mechanisms.MechanismError("native workspace configuration hash differs")
        from closer_quarantine import workspace_bindings

        def unique(pairs):
            value = {}
            for key, entry in pairs:
                if key in value:
                    raise mechanisms.MechanismError(
                        "native workspace configuration has duplicate keys"
                    )
                value[key] = entry
            return value

        workspace_bindings(json.loads(workspace_bytes, object_pairs_hook=unique))
        # Pin the exact canonical routing map before A. Product checkout liveness
        # and Git identity are checked only by callbacks that consume that checkout.
        load_workspace_config(
            source, pin["sha256"], require_pin=True, targets=targets, validate_execution=False
        )
        workspace_path = directory / "workspaces.json"
        _immutable(workspace_path, workspace_bytes)
        workspace_config = {
            "path": str(workspace_path),
            "sha256": pin["sha256"],
            "source_path": str(source),
        }
    finite = policy["schema"] == "native-scoped-capacity/v2"
    frontier = None
    if finite:
        from closer_frontier_policy import validate_frontier

        frontier = validate_frontier(cfg.get("finite_frontier"), targets=targets)
        if (
            frontier != cfg.get("finite_frontier")
            or frontier["owner"] != doc["owner"]
            or cfg.get("request_timeout_seconds") != policy["request_timeout_seconds"]
            or invocation.get("execution_policy") != frontier["schema"]
            or "original_deadline" in invocation
        ):
            raise mechanisms.MechanismError("native finite frontier differs from admitted campaign")
    elif cfg.get("finite_frontier") is not None:
        raise mechanisms.MechanismError("finite frontier requires explicit native v2 publication")
    # The producer's public API reserves the global capacity.json basename.
    report_path = directory / "scoped-capacity.json"
    producer_cfg = {
        "schema": scoped.SCHEMA,
        "owner": policy["owner"],
        "profile_ids": [] if control_plane_only else policy["profile_ids"],
        "bounded_trial_agents": [] if control_plane_only else policy["bounded_trial_agents"],
        **({"control_plane_only": True} if control_plane_only else {}),
        # This report window limits evidence freshness, never frontier work duration.
        "original_deadline": (
            time.time() + policy["freshness_seconds"] if finite else invocation["original_deadline"]
        ),
        "runtime_manifest": str(Path(manifest_path).resolve()),
        "runtime_manifest_sha256": _hash(Path(manifest_path).resolve()),
    }
    if not control_plane_only:
        try:
            scoped.evaluate(producer_cfg)
        except (scoped.CapacityError, OSError, ValueError) as exc:
            control_plane_reasons.append(
                f"optional model telemetry unavailable: {type(exc).__name__}: {exc}"
            )
            # Unavailable optional provider telemetry cannot qualify model work,
            # but does not prevent independently authenticated GitHub control IO.
            producer_cfg.update(control_plane_only=True, profile_ids=[], bounded_trial_agents=[])
    report = scoped.publish(producer_cfg, report_path, expected_sha256=None)
    if not scoped.validate_report(report, now=time.time()):
        raise mechanisms.MechanismError("native scoped capacity readback is not current")
    context = {
        "schema": "native-invocation-admission/v2" if finite else "native-invocation-admission/v1",
        "nonce": nonce,
        "started_at": invocation["started_at"],
        "campaign": _campaign_identity(state_path, doc),
        "assignment": {
            "path": str(assignment),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "source_path": str(assignment_source),
        },
        "report": {"path": str(report_path), "sha256": _hash(report_path)},
        "runtime_manifest": producer_cfg["runtime_manifest"],
        "runtime_manifest_sha256": producer_cfg["runtime_manifest_sha256"],
    }
    if demand_reference is not None:
        context["demand_assignment"] = dict(demand_reference)
        context["demand_continuations"] = list(doc.get("demand_continuations", []))
    if control_plane_reasons:
        context["control_plane_reasons"] = control_plane_reasons
    if workspace_config is not None:
        context["workspace_config"] = workspace_config
    if finite:
        context["finite_frontier"] = frontier
        context["request_timeout_seconds"] = policy["request_timeout_seconds"]
    else:
        context["original_deadline"] = invocation["original_deadline"]
    path = directory / "admission.json"
    if path.exists() or path.is_symlink():
        raise mechanisms.MechanismError("native admission already exists; no replacement")
    _signed(path, {"binding": binding(manifest), "context": context}, root)
    invocation["context"] = context
    return context


def _campaign_identity(state_path, doc):
    return {
        "state": str(Path(state_path).resolve()),
        "campaign_id": doc["campaign_id"],
        "owner": doc["owner"],
        "generation": doc.get("generation", 1),
    }


def validate_admission(manifest, context, *, for_effects, _scope=None, for_test=False):
    import closer_operational_capacity as scoped

    private = "private_test" in (manifest.get("mechanism_contract") or {})
    if private and for_effects and not for_test:
        raise mechanisms.MechanismError(
            "private native TEST admission cannot authorize business effects"
        )
    if for_test and not private:
        raise mechanisms.MechanismError(
            "test-only admission selection requires private TEST source"
        )
    policy = capacity_contract(manifest, control_plane_only=True)
    root = contract(manifest, _scope=_scope)[3]
    if not policy or not isinstance(context, dict):
        raise mechanisms.MechanismError("native signed admission missing")
    nonce = context.get("nonce", "")
    if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce):
        raise mechanisms.MechanismError("native admission nonce invalid")
    directory = root / nonce
    if directory.resolve() != directory or directory.is_symlink():
        raise mechanisms.MechanismError("native admission path escaped")
    signed = _read(directory / "admission.json")
    if (
        signed.get("record") != {"binding": binding(manifest, _scope=_scope), "context": context}
        or not hmac.compare_digest(
            signed["signature"], mechanisms._signature(signed["record"], mechanisms._key(root))
        )
        or context.get("schema")
        != (
            "native-invocation-admission/v2"
            if policy["schema"] == "native-scoped-capacity/v2"
            else "native-invocation-admission/v1"
        )
    ):
        raise mechanisms.MechanismError("native admission signature/binding differs")
    start, deadline = context.get("started_at"), context.get("original_deadline")
    finite = policy["schema"] == "native-scoped-capacity/v2"
    if isinstance(start, bool) or not isinstance(start, (int, float)) or not math.isfinite(start):
        raise mechanisms.MechanismError("native admission start invalid")
    if finite:
        from closer_dispatcher import targets_from_assignment
        from closer_frontier_policy import validate_frontier

        frontier = validate_frontier(
            context.get("finite_frontier"),
            targets=targets_from_assignment(Path(context["assignment"]["path"])),
        )
        if (
            frontier != context.get("finite_frontier")
            or frontier["owner"] != context["campaign"]["owner"]
            or context.get("request_timeout_seconds") != policy["request_timeout_seconds"]
            or "original_deadline" in context
        ):
            raise mechanisms.MechanismError("native signed finite frontier differs")
    elif (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(deadline)
        or deadline != start + policy["admission_seconds"]
        or "finite_frontier" in context
    ):
        raise mechanisms.MechanismError("native original admission deadline differs")
    source = private_test_assignment_source(manifest)
    for key, name in (("assignment", "assignment.json"), ("report", "scoped-capacity.json")):
        pin = context[key]
        path = directory / name
        if pin.get("path") != str(path) or path.is_symlink() or _hash(path) != pin.get("sha256"):
            raise mechanisms.MechanismError("native immutable admission artifact differs")
    if "workspace_config" in context:
        pin = context["workspace_config"]
        path = directory / "workspaces.json"
        if (
            not isinstance(pin, dict)
            or set(pin) != {"path", "sha256", "source_path"}
            or pin.get("path") != str(path)
            or path.is_symlink()
            or hashlib.sha256(_bounded_bytes(path, 1024 * 1024)).hexdigest() != pin.get("sha256")
            or not isinstance(pin.get("source_path"), str)
            or not Path(pin["source_path"]).is_absolute()
        ):
            raise mechanisms.MechanismError("native immutable workspace configuration differs")
    if "private_test" in manifest["mechanism_contract"] and any(
        key in context for key in ("coding_continuation", "demand_assignment")
    ):
        raise mechanisms.MechanismError("private native test cannot use production continuation")
    if "coding_continuation" in context:
        _value, _record, _document = _coding_record_from_context(manifest, context)
        selector = context["coding_continuation"]
        expected_assignment = json.dumps(
            {
                "schema": "closer-coding-assignment/v1",
                "reference": selector["reference"],
                "source_target": selector["source_target"],
                "parent_target": selector["target"],
                "targets": [{"target": selector["coding_target"]}],
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        if (
            context["assignment"].get("source_path") != context["assignment"]["path"]
            or _bounded_bytes(Path(context["assignment"]["path"]), 1024 * 1024)
            != expected_assignment
        ):
            raise mechanisms.MechanismError("native coding assignment source differs")
    elif "demand_assignment" in context:
        demand_source, demand_raw = _demand_assignment_bytes(
            manifest,
            context["demand_assignment"],
            context["campaign"],
            continuations=context.get("demand_continuations", []),
            finite_frontier=context.get("finite_frontier"),
        )
        if (
            context["assignment"].get("source_path") != str(demand_source)
            or hashlib.sha256(demand_raw).hexdigest() != context["assignment"]["sha256"]
        ):
            raise mechanisms.MechanismError("native typed demand assignment origin differs")
    elif context["assignment"].get("source_path") != str(source):
        raise mechanisms.MechanismError("native assignment source differs")
    manifest_path = Path(context["runtime_manifest"])
    if (
        manifest_path.resolve() != manifest_path
        or _hash(manifest_path) != context["runtime_manifest_sha256"]
    ):
        raise mechanisms.MechanismError("native admission manifest bytes differ")
    capacity_report = _read(directory / "scoped-capacity.json")
    if capacity_report.get("control_plane_only") is not True:
        capacity_contract(manifest)  # Model qualification retains the strict registry contract.
    if for_effects and (
        (not finite and not time.time() < deadline)
        or not scoped.validate_report(_read(directory / "scoped-capacity.json"), now=time.time())
    ):
        raise mechanisms.MechanismError("native admission expired or operational inputs changed")
    return context


def admission_config(manifest, cfg, context, *, for_effects=True):
    """Derive only this worker's immutable cfg; never rewrite campaign history."""
    import copy

    validate_admission(manifest, context, for_effects=for_effects)
    result = copy.deepcopy(cfg)
    if "coding_continuation" in context:
        selector = context["coding_continuation"]
        result["finite_frontier"] = copy.deepcopy(selector["finite_frontier"])
        result["request_timeout_seconds"] = selector["request_timeout_seconds"]
        result["dispatch_state"] = selector["queue_state"]
        result["coding_continuation"] = copy.deepcopy(selector["reference"])
        # Continue the original ordinary field validation against this actual
        # signed private-worker configuration, never a modified campaign doc.
        cfg = result
    workspace = context.get("workspace_config")
    source_pin = (
        {"path": workspace["source_path"], "sha256": workspace["sha256"]}
        if workspace is not None
        else None
    )
    if for_effects and cfg.get("workspaces") != source_pin:
        raise mechanisms.MechanismError(
            "native workspace configuration differs from signed admission"
        )
    if workspace is not None:
        result["workspaces"] = {key: workspace[key] for key in ("path", "sha256")}
    else:
        result.pop("workspaces", None)
    result["assignment"] = context["assignment"]["path"]
    result["capacity_json"] = result["capacity_report"] = context["report"]["path"]
    if context["schema"] == "native-invocation-admission/v2":
        if (
            cfg.get("finite_frontier") != context["finite_frontier"]
            or cfg.get("request_timeout_seconds") != context["request_timeout_seconds"]
        ):
            raise mechanisms.MechanismError(
                "native worker finite frontier differs from signed admission"
            )
        for key in ("original_deadline", "budget_seconds", "dispatch_budget_seconds"):
            result.pop(key, None)
        result["finite_frontier"] = copy.deepcopy(context["finite_frontier"])
        result["request_timeout_seconds"] = context["request_timeout_seconds"]
        result["execution_policy"] = context["finite_frontier"]["schema"]
    else:
        if cfg.get("finite_frontier") is not None:
            raise mechanisms.MechanismError("legacy admission cannot authorize finite frontier")
        result["original_deadline"] = context["original_deadline"]
        remaining = (
            context["original_deadline"] - time.time()
            if for_effects
            else context["original_deadline"] - context["started_at"]
        )
        reserve = max(15.0, float(cfg.get("receipt_reserve_seconds", 300)))
        inner = math.floor(remaining - reserve - 300 - 15)
        if for_effects and inner <= 0:
            raise mechanisms.MechanismError(
                "native original admission has no B/C/inner receipt budget"
            )
        result["budget_seconds"] = remaining
        result["dispatch_budget_seconds"] = inner
    result["native_hooks"]["postrun_dependencies"]["assignment"] = {
        key: context["assignment"][key] for key in ("path", "sha256")
    }
    result["native_admission"] = context
    return result


def result_admission_config(cfg, context, state_path, doc, worker):
    """Recover a completed worker's original context, not the latest invocation."""
    from closer_campaign import load_manifest

    manifest = load_manifest(context["runtime_manifest"])
    validate_admission(manifest, context, for_effects=False)
    if (
        doc.get("release_pin") != manifest["package_digest"]
        or context["campaign"] != _campaign_identity(state_path, doc)
        or (
            cfg.get("runtime_manifest") is not None
            and str(Path(cfg["runtime_manifest"]).resolve()) != context["runtime_manifest"]
        )
    ):
        raise mechanisms.MechanismError("native result admission belongs to another campaign/pin")
    root = contract(manifest)[3]
    signed = _read(root / context["nonce"] / ("worker-" + worker["worker_id"] + ".json"))
    record = signed["record"]
    expected = {
        "state": str(state_path),
        "target": worker["target"],
        "owner": doc["owner"],
        "generation": str(doc.get("generation", 1)),
        "result": str(Path(str(state_path) + ".results") / (worker["worker_id"] + ".json")),
        "runtime-manifest": context["runtime_manifest"],
        "mechanism": mechanisms.NATIVE,
    }
    if (
        not hmac.compare_digest(
            signed["signature"], mechanisms._signature(record, mechanisms._key(root))
        )
        or record.get("binding") != binding(manifest)
        or record.get("admission") != context
        or record.get("fields") != expected
    ):
        raise mechanisms.MechanismError("native result admission lacks exact pre-launch handoff")
    result = admission_config(manifest, cfg, context, for_effects=False)
    result["native_worker"] = expected
    return result


def pre_dispatch_no_start_proof(manifest, cfg, candidate, *, record_boundary=False):
    """Seal/recheck this worker's positive no-B and exact native cleanup evidence.

    This never settles an older plaintext UNKNOWN. The producer must observe both
    phases in its own invocation, and the reader requires its create-only signed
    boundary in addition to the original signed worker handoff.
    """
    try:
        context = validate_admission(manifest, cfg["native_admission"], for_effects=False)
        schema = context.get("schema")
        if schema not in {"native-invocation-admission/v1", "native-invocation-admission/v2"}:
            return None
        finite = schema == "native-invocation-admission/v2"
        fields = cfg["native_worker"]
        result = Path(fields["result"])
        worker_id = result.stem
        if (
            not re.fullmatch(r"[0-9a-f]{12}", worker_id)
            or fields.get("mechanism") != mechanisms.NATIVE
            or fields.get("runtime-manifest") != context["runtime_manifest"]
            or result != Path(fields["state"] + ".results") / (worker_id + ".json")
            or result.resolve() != result
        ):
            return None
        root = contract(manifest)[3]
        directory = root / context["nonce"]
        if finite:
            watermark = directory / "action-b-receipt-watermark.json"
            if watermark.exists() or watermark.is_symlink():
                return None
        handoff = _read(directory / ("worker-" + worker_id + ".json"))
        original = handoff["record"]
        if (
            original.get("binding") != binding(manifest)
            or original.get("admission") != context
            or original.get("fields") != fields
            or original.get("schema") != "native-worker-handoff/v1"
            or original.get("nonce") != context["nonce"]
            or not hmac.compare_digest(
                handoff["signature"], mechanisms._signature(original, mechanisms._key(root))
            )
            or not isinstance(candidate, dict)
        ):
            return None
        phases = candidate["phases"]
        if (
            not isinstance(phases, dict)
            or set(phases) != {"A", "B", "C"}
            or any(not isinstance(phases.get(name), dict) for name in ("A", "B", "C"))
        ):
            return None
        if any("denial" in phase or "error" in phase for phase in phases.values()):
            return None
        execution = candidate["execution"]
        if (
            not isinstance(execution, dict)
            or set(execution)
            != {
                "schema",
                "worker",
                "admission_nonce",
                "admission_started_at",
                "runtime_binding_entered_at",
                "runtime_binding_ready_at",
                "phases",
            }
            or execution.get("schema") != "native-round-execution/v1"
            or execution.get("worker") != fields
            or execution.get("admission_nonce") != context["nonce"]
            or execution.get("admission_started_at") != context["started_at"]
            or execution.get("phases") != phases
        ):
            return None
        cleanup = phases["C"]["cleanup"]
        token_hash = phases["A"]["token_sha256"]
        lane = (
            Path(
                cfg.get("handoff_dir")
                or os.environ.get("HANDOFF_DIR")
                or Path.home() / ".codex/handoff"
            )
            / ".lane-closer.lock"
        )
        times = [
            phases[phase].get(key)
            for phase, key in (
                ("A", "started_at"),
                ("A", "finished_at"),
                ("B", "checked_at"),
                ("C", "started_at"),
                ("C", "finished_at"),
            )
        ]
        entry_times = [
            execution.get(key) for key in ("runtime_binding_entered_at", "runtime_binding_ready_at")
        ]
        if (
            any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                for value in [*entry_times, *times]
            )
            or not context["started_at"] <= entry_times[0] <= entry_times[1] <= times[0]
            or not context["started_at"] <= times[0] <= times[1] <= times[2] <= times[3] <= times[4]
            or (not finite and not times[4] <= context["original_deadline"])
        ):
            return None
        if (
            type(phases["A"].get("exit_code")) is not int
            or phases["A"].get("exit_code") != 0
            or phases["A"].get("admitted") is not True
            or phases["A"].get("attempted") is not True
            or type(phases["A"].get("lease_inode")) is not int
            or phases["A"]["lease_inode"] <= 0
            or not re.fullmatch(r"[0-9a-f]{64}", token_hash)
            or phases["B"].get("started") is not False
            or phases["B"].get("reason") != "pre_dispatch_binding_refusal"
            or phases["B"].get("started_at") is not None
            or phases["B"].get("finished_at") is not None
            or phases["C"].get("attempted") is not True
            or phases["C"].get("exit_code") != 65
            or phases["C"].get("dependencies_valid") is not True
            or phases["C"].get("refusal") != "terminal_named_receipt_unavailable"
            or not isinstance(cleanup, dict)
            or cleanup.get("schema") != "closer-native-lease-cleanup/v1"
            or cleanup.get("lane") != "closer"
            or cleanup.get("lane_dir") != str(lane)
            or cleanup.get("expected_token_sha256") != token_hash
            or cleanup.get("meta_sha256") != token_hash
            or cleanup.get("released") is not True
            or cleanup.get("exact_token_matched") is not True
            or type(cleanup.get("released_lease_inode")) is not int
            or cleanup["released_lease_inode"] != phases["A"]["lease_inode"]
            or isinstance(cleanup.get("released_at"), bool)
            or not isinstance(cleanup.get("released_at"), (int, float))
            or not math.isfinite(cleanup["released_at"])
            or not times[3] <= cleanup["released_at"] <= times[4]
        ):
            return None
        proof = {
            "schema": (
                "native-pre-dispatch-no-start/v2" if finite else "native-pre-dispatch-no-start/v1"
            ),
            "worker": fields,
            "admission_nonce": context["nonce"],
            "assignment_sha256": context["assignment"]["sha256"],
            "phases": phases,
            "execution": execution,
            "accepted": False,
        }
        if finite:
            # This evidence authenticates an observed absence of B.  It does
            # not refresh the v2 report or confer any new effect authority.
            proof["finite_frontier"] = context["finite_frontier"]
            proof["request_timeout_seconds"] = context["request_timeout_seconds"]
            boundary = directory / ("pre-dispatch-no-start-v2-" + worker_id + ".json")
        else:
            proof["original_deadline"] = context["original_deadline"]
            boundary = directory / ("pre-dispatch-no-start-" + worker_id + ".json")
        record = {"binding": binding(manifest), "proof": proof}
        if record_boundary:
            observed_now = time.time()
            if (
                not math.isfinite(observed_now)
                or _CURRENT_ENTRY.get(str(root)) != context["nonce"]
                or not times[4] <= observed_now
                or (not finite and not observed_now < context["original_deadline"])
                or result.exists()
                or result.is_symlink()
                or boundary.exists()
                or boundary.is_symlink()
            ):
                return None
            _immutable(
                boundary,
                json.dumps(
                    {
                        "record": record,
                        "signature": mechanisms._signature(record, mechanisms._key(root)),
                    },
                    sort_keys=True,
                ).encode(),
            )
        else:
            signed = json.loads(_bounded_bytes(boundary, 128 * 1024))
            if (
                not isinstance(signed, dict)
                or candidate != proof
                or signed.get("record") != record
                or not hmac.compare_digest(
                    signed["signature"], mechanisms._signature(record, mechanisms._key(root))
                )
            ):
                return None
        return proof
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return None


def completed_round_proof(manifest, cfg, candidate, *, record_boundary=False):
    """Seal real B0/C0 completion, then revalidate only that exact known domain."""
    import closer_pipeline
    import closer_resources
    from closer_dispatcher import targets_from_assignment

    try:
        context = cfg["native_admission"]
        validate_admission(manifest, context, for_effects=False)
        if not isinstance(candidate, dict) or cfg["assignment"] != context["assignment"]["path"]:
            return None
        round_id = candidate.get("round_id")
        if not isinstance(round_id, str) or not re.fullmatch(r"[0-9a-f]{16,64}", round_id):
            return None
        state = Path(str(cfg["dispatch_state"]) + ".pipeline.json")
        root = closer_resources.scoped_root(state)
        receipt = root / "rounds" / (round_id + ".json")
        if (
            receipt.resolve() != receipt
            or receipt.is_symlink()
            or candidate.get("receipt_path") != str(receipt)
        ):
            return None
        raw = _bounded_bytes(receipt, 32 * 1024 * 1024)
        digest = hashlib.sha256(raw).hexdigest()
        if candidate.get("receipt_sha256", digest) != digest:
            return None
        aggregate = json.loads(raw)
        frontier = context.get("finite_frontier")
        if (
            aggregate.get("finite_frontier") != frontier
            or candidate.get("finite_frontier") != frontier
        ):
            return None
        targets = targets_from_assignment(Path(cfg["assignment"]))
        verdict = closer_resources.validate_stop(state, round_id, targets)
        if (
            verdict.get("round_id") != round_id
            or verdict.get("receipt_sha256") != digest
            or verdict.get("scoped_completion_valid") is not True
            or verdict.get("admitted_domain_valid") is not True
            or verdict.get("admitted_domain_reasons")
            or set(verdict.get("run_ids", {})) != set(targets)
        ):
            return None
        row_hashes = {}
        for target, run_id in verdict["run_ids"].items():
            shard = closer_resources.shard_dir(root, target) / "state.json"
            shard_doc = closer_pipeline._load(shard)
            row = (
                shard_doc["runs"][run_id]["targets"][target]
                if frontier is None
                else shard_doc["frontiers"][frontier["frontier_id"]]["items"][target]
            )
            if aggregate["targets"][target] != row or aggregate["run_ids"][target] != run_id:
                return None
            row_hashes[target] = mechanisms.digest(row)
        handoff = Path(
            cfg.get("handoff_dir")
            or os.environ.get("HANDOFF_DIR")
            or Path.home() / ".codex/handoff"
        )
        lane = handoff / ".lane-closer.lock"
        if lane.exists() or lane.is_symlink():
            return None
        proof = {
            "schema": "native-completed-round/v1",
            "round_id": round_id,
            "receipt_path": str(receipt),
            "receipt_sha256": digest,
            "run_ids": verdict["run_ids"],
            "row_sha256": row_hashes,
            "assignment_sha256": context["assignment"]["sha256"],
            "admission_nonce": context["nonce"],
            "boundary_global_stop_valid": True,
            "accepted": False,
        }
        if frontier is not None:
            proof["finite_frontier"] = frontier
        directory = contract(manifest)[3]
        boundary = directory / context["nonce"] / ("completed-round-" + round_id + ".json")
        record = {"binding": binding(manifest), "proof": proof}
        if record_boundary:
            if verdict.get("global_stop_valid") is not True or verdict.get("valid") is not True:
                return None
            if boundary.exists() or boundary.is_symlink():
                return None
            _signed(boundary, record, directory)
        else:
            signed = _read(boundary)
            if (
                signed.get("record") != record
                or candidate != proof
                or not hmac.compare_digest(
                    signed["signature"], mechanisms._signature(record, mechanisms._key(directory))
                )
            ):
                return None
        return proof
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return None


def rearm_on_hold_followup(campaign, doc, manifest):
    """A same-assignment continuation needs an exact audited no-start release."""
    import closer_hold_followup

    return closer_hold_followup.rearm(campaign, doc, manifest)


def rearm_on_assignment_change(campaign, doc, manifest):
    """Standing mode only: a different assignment may follow a proven completed round."""
    policy = capacity_contract(manifest, control_plane_only=True)
    if (
        not policy
        or policy.get("schema") == "native-scoped-capacity/v2"
        or policy.get("continue_on_assignment_change") is not True
    ):
        return doc
    if (
        doc.get("phase") != "work"
        or doc.get("hold")
        or doc.get("release_record")
        or doc.get("targets") != {"*round*": "done"}
    ):
        return doc
    workers = doc.get("workers") or []
    if (
        not workers
        or any(
            w.get("state") != "terminal"
            or w.get("outcome") not in {"ok", "unavailable", "scoped_continuation"}
            for w in workers
        )
        or workers[-1].get("outcome") != "ok"
    ):
        return doc
    last = workers[-1]
    result_path = campaign.path.with_name(campaign.path.name + ".results") / (
        last["worker_id"] + ".json"
    )
    result = _read(result_path)
    if (
        result.get("worker_id") != last["worker_id"]
        or result.get("target") != "*round*"
        or result.get("mechanism") != mechanisms.NATIVE
        or result.get("outcome") != "ok"
    ):
        raise mechanisms.MechanismError("native standing completion identity is unavailable")
    context = result["native_admission"]
    cfg = result_admission_config(doc["worker_config"], context, campaign.path, doc, last)
    candidate = json.loads(result["detail"])["completed_round"]
    if (
        not isinstance(candidate, dict)
        or completed_round_proof(manifest, cfg, candidate) != candidate
    ):
        raise mechanisms.MechanismError(
            "native standing original completion no longer proves known outcomes"
        )
    source = Path(context["assignment"]["source_path"])
    if source.resolve() != source or source.is_symlink():
        raise mechanisms.MechanismError("native standing controller assignment is aliased")
    observed = hashlib.sha256(_bounded_bytes(source, 2 * 1024 * 1024)).hexdigest()
    if observed == context["assignment"]["sha256"]:
        return doc
    expected_revision = doc["revision"]

    def mutate(current):
        if (
            current["revision"] != expected_revision
            or current.get("hold")
            or current.get("targets") != {"*round*": "done"}
            or current.get("phase") != "work"
            or current.get("release_record")
            or current.get("workers") != workers
            or hashlib.sha256(_bounded_bytes(source, 2 * 1024 * 1024)).hexdigest() != observed
            or completed_round_proof(manifest, cfg, candidate) != candidate
        ):
            raise mechanisms.MechanismError("native standing state/proof changed before transition")
        previous_generation = current.get("generation", 1)
        current.setdefault("assignment_transitions", []).append(
            {
                "schema": "native-assignment-transition/v1",
                "previous_worker_id": last["worker_id"],
                "previous_admission_nonce": context["nonce"],
                "completed_round": candidate,
                "previous_assignment_sha256": context["assignment"]["sha256"],
                "observed_assignment_sha256": observed,
                "source_path": str(source),
                "previous_generation": previous_generation,
                "next_generation": previous_generation + 1,
                "accepted": False,
            }
        )
        current["generation"] = previous_generation + 1
        current["targets"]["*round*"] = "pending"

    return campaign.update(mutate)


def binding(manifest, *, _scope=None):
    value, _wrapper, _python, _root = contract(manifest, _scope=_scope)
    return _binding_from_contract(manifest, value)


def _binding_from_contract(manifest, value):
    """Pure projection of an already validated local contract; no authority."""
    return {
        "mechanism": mechanisms.NATIVE,
        "mechanism_contract_digest": mechanisms.digest(value),
        "policy_digest": mechanisms.digest(manifest["policy"]),
        **{key: manifest.get(key) for key in mechanisms.PIN_KEYS},
    }


def wrapper_block(manifest_path, campaign_root, interpreter, launcher):
    """Reviewed opt-in source text; caller writes only a preview, never a host file."""
    args = [
        str(interpreter),
        str(launcher),
        "native-entry",
        "--manifest",
        str(manifest_path),
        "--root",
        str(campaign_root),
    ]
    return (
        MARKER + "\n"
        'if [[ "$LANE" == "closer" && -f "$HOME/.codex/handoff/closer-native-handoff.enabled" ]]; then\n'
        "  " + shlex.join(args) + "\n"
        '  exit "$?"\nfi\n' + END_MARKER + "\n"
    )


def render_cursor_wrapper(source, *, manifest_path, campaign_root, interpreter, launcher):
    """Return opt-in wrapper bytes, before legacy capacity/auth/provider discovery."""
    if MARKER in source or END_MARKER in source:
        raise mechanisms.MechanismError("wrapper already has a native entry block")
    anchor = 'export PATH="$HOME/.local/bin:$HOME/.cursor/bin:$PATH"'
    if (
        source.count(anchor) != 1
        or 'case "$LANE" in' not in source
        or not source.startswith("#!/bin/bash\n")
    ):
        raise mechanisms.MechanismError("unsupported Cursor wrapper source")
    return source.replace(
        anchor,
        wrapper_block(manifest_path, campaign_root, interpreter, launcher) + "\n" + anchor,
        1,
    )


def _entry_argv(manifest, manifest_path):
    value, _wrapper, python, _root = contract(manifest)
    return [
        str(python),
        manifest["launcher"]["path"],
        "native-entry",
        "--manifest",
        str(manifest_path),
        "--root",
        value["campaign_root"],
    ]


def _check_wrapper_entry(manifest, manifest_path):
    value, wrapper, python, root = contract(manifest)
    expected = wrapper_block(
        manifest_path, value["campaign_root"], python, manifest["launcher"]["path"]
    )
    if wrapper.read_text().count(expected) != 1:
        raise mechanisms.MechanismError("wrapper does not hand off to this exact pinned entry")
    if not (Path.home() / ".codex/handoff/closer-native-handoff.enabled").is_file():
        raise mechanisms.MechanismError("native wrapper opt-in is absent")
    return value, wrapper, python, root


def _write(path, value):
    mechanisms._write(path, value)


def _read(path):
    return mechanisms._read(path)


def _wait(path, deadline):
    return mechanisms._wait(path, deadline)


def _signed(path, value, root):
    _write(
        path, {"record": value, "signature": mechanisms._signature(value, mechanisms._key(root))}
    )


_CLEANUP_POLICY = {"schema": "closer-native-correction-cleanup/v1"}


def correction_cleanup_enabled(cfg):
    policy = cfg.get("policy")
    return (
        type(policy) is dict
        and policy.get("native_correction_cleanup") == _CLEANUP_POLICY
        and type(policy.get("correction_controller_installation")) is dict
    )


def _existing_lifecycle_key(root):
    """Read existing native custody key only; never create a signer identity."""
    path = Path(root) / "supervisor.key"
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size != 32
        ):
            raise mechanisms.MechanismError("existing native lifecycle key unsafe")
        key = os.read(descriptor, 33)
        after = os.fstat(descriptor)
        named = path.stat(follow_symlinks=False)
        fields = (
            "st_dev",
            "st_ino",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
            "st_mode",
            "st_uid",
            "st_nlink",
        )
        if len(key) != 32 or any(
            getattr(before, name) != getattr(other, name)
            for other in (after, named)
            for name in fields
        ):
            raise mechanisms.MechanismError("existing native lifecycle key changed")
        return key
    finally:
        os.close(descriptor)


def _lifecycle_authority(manifest, cfg):
    """Original signed worker handoff, not a current-entry or cleanup exemption."""
    if not correction_cleanup_enabled(cfg) or cfg.get("policy") != manifest.get("policy"):
        raise mechanisms.MechanismError("source-selected correction cleanup policy absent")
    context = validate_admission(manifest, cfg["native_admission"], for_effects=False)
    if context.get("schema") != "native-invocation-admission/v2":
        raise mechanisms.MechanismError("correction cleanup requires original finite admission")
    fields = cfg["native_worker"]
    worker_id = Path(fields["result"]).stem
    if not re.fullmatch(r"[a-f0-9]{12}", worker_id):
        raise mechanisms.MechanismError("original native cleanup worker malformed")
    root = contract(manifest)[3]
    directory = root / context["nonce"]
    if directory.resolve() != directory or directory.parent != root:
        raise mechanisms.MechanismError("original native cleanup custody redirected")
    key = _existing_lifecycle_key(root)
    path = directory / ("worker-" + worker_id + ".json")
    raw = _bounded_bytes(path, 1048576)
    signed = json.loads(raw)
    record = signed["record"]
    if (
        record.get("schema") != "native-worker-handoff/v1"
        or record.get("binding") != binding(manifest)
        or record.get("nonce") != context["nonce"]
        or record.get("admission") != context
        or record.get("fields") != fields
        or not hmac.compare_digest(
            str(signed.get("signature", "")), mechanisms._signature(record, key)
        )
    ):
        raise mechanisms.MechanismError("original native cleanup handoff differs")
    return directory, key, context, fields, worker_id, hashlib.sha256(raw).hexdigest()


def _read_lifecycle_record(directory, name, key):
    raw = _bounded_bytes(directory / name, 1048576)
    signed = json.loads(raw)
    record = signed["record"]
    if not hmac.compare_digest(
        str(signed.get("signature", "")), mechanisms._signature(record, key)
    ):
        raise mechanisms.MechanismError("native lifecycle signature differs")
    return record, {"path": str(directory / name), "sha256": hashlib.sha256(raw).hexdigest()}


def source_b_start(installation):
    """Authenticate this actual source child's original parent-owned B start."""
    from closer_campaign import load_manifest

    manifest = load_manifest(installation.manifest_path)
    context = installation.admission
    policy = manifest.get("policy") or {}
    if policy.get("native_correction_cleanup") != _CLEANUP_POLICY:
        return None
    validate_admission(manifest, context, for_effects=True)
    root = contract(manifest)[3]
    directory = root / context["nonce"]
    if directory.resolve() != directory or directory.parent != root or not directory.is_dir():
        raise mechanisms.MechanismError("native B start custody redirected")
    key = _existing_lifecycle_key(root)
    names = sorted(p.name for p in directory.iterdir() if p.name.startswith("worker-b-start-"))
    if len(names) > 16:
        raise mechanisms.MechanismError("native B start inventory saturated")
    matched = []
    for name in names:
        if not re.fullmatch(r"worker-b-start-[a-f0-9]{12}\.json", name):
            raise mechanisms.MechanismError("native B start name malformed")
        record, ref = _read_lifecycle_record(directory, name, key)
        if record.get("process", {}).get("pid") != os.getpid():
            continue
        worker_id = name[len("worker-b-start-") : -len(".json")]
        fields = record.get("worker")
        if type(fields) is not dict:
            raise mechanisms.MechanismError("native B start worker unavailable")
        _directory, _key, _context, _fields, _id, handoff_sha = _lifecycle_authority(
            manifest, {"policy": policy, "native_admission": context, "native_worker": fields}
        )
        import closer_correction_controller as controller_source

        doc = controller_source.IndexController._physical_state(
            installation.config["state_path"], "original worker configuration redirected"
        )
        if "coding_continuation" in context:
            import closer_coding_continuation as continuation

            selector = context["coding_continuation"]
            registry = continuation.validate_registry(doc.get(continuation.REGISTRY_FIELD))
            row = registry.get(selector["reference"]["stable_key"])
            workers = (
                [row["worker"]]
                if row is not None
                and continuation._canonical(row["worker"]) == continuation._canonical(selector)
                and row["state"] in {"launch_intent", "submitted", "active"}
                and selector["worker_id"] == worker_id
                else []
            )
        else:
            workers = [
                worker for worker in doc.get("workers", []) if worker.get("worker_id") == worker_id
            ]
        if len(workers) != 1:
            raise mechanisms.MechanismError("original source worker identity unavailable")
        original_cfg = result_admission_config(
            doc["worker_config"], context, Path(installation.config["state_path"]), doc, workers[0]
        )
        if (
            record.get("schema") != "closer-native-correction-b-start/v1"
            or record.get("binding") != binding(manifest)
            or record.get("admission") != context
            or record.get("worker_id") != worker_id
            or _id != worker_id
            or record.get("handoff_sha256") != handoff_sha
            or record.get("config_sha256") != mechanisms.digest(installation.config)
            or record.get("worker_config_sha256") != mechanisms.digest(original_cfg)
            or record.get("producer_sha256") != manifest["modules"]["closer_runtime_binding.py"]
            or record.get("process", {}).get("process_group") != os.getpid()
            or not isinstance(record.get("process", {}).get("os_started"), str)
        ):
            raise mechanisms.MechanismError("native B start differs from original source child")
        matched.append(ref)
    if len(matched) != 1:
        raise mechanisms.MechanismError("source awaits exact native B startup receipt")
    return matched[0]


def retained_correction_cleanup(manifest, campaign_doc, plan):
    """Authenticate prospective old B/C custody only, never current authority."""
    import closer_campaign as campaign_source
    import closer_correction_controller as ctl

    origin = plan["native_lifecycle"]
    ref = origin["native_b_start"]
    root = contract(manifest)[3]
    path = Path(ref["path"])
    if (
        path.resolve() != path
        or path.parent.parent != root
        or not re.fullmatch(r"[a-f0-9]{32}", path.parent.name)
        or not re.fullmatch(r"worker-b-start-[a-f0-9]{12}\.json", path.name)
    ):
        raise mechanisms.MechanismError("retained cleanup selector escaped original native custody")
    key = _existing_lifecycle_key(root)
    start, exact_ref = _read_lifecycle_record(path.parent, path.name, key)
    if ref != exact_ref or start.get("admission", {}).get("nonce") != path.parent.name:
        raise mechanisms.MechanismError("retained cleanup B-start bytes differ")
    context, fields = start["admission"], start["worker"]
    worker_id = start["worker_id"]
    workers = [row for row in campaign_doc.get("workers", []) if row.get("worker_id") == worker_id]
    if len(workers) != 1 or workers[0].get("state") != "terminal":
        raise mechanisms.MechanismError("retained source worker is not independently terminal")
    original_cfg = result_admission_config(
        campaign_doc["worker_config"],
        context,
        Path(context["campaign"]["state"]),
        campaign_doc,
        workers[0],
    )
    directory, original_key, _context, expected_fields, expected_id, handoff_sha = (
        _lifecycle_authority(manifest, original_cfg)
    )
    if (
        directory != path.parent
        or original_key != key
        or expected_fields != fields
        or expected_id != worker_id
        or start.get("handoff_sha256") != handoff_sha
        or start.get("binding") != binding(manifest)
        or start.get("schema") != "closer-native-correction-b-start/v1"
        or start.get("worker_config_sha256") != mechanisms.digest(original_cfg)
        or start.get("producer_sha256") != manifest["modules"]["closer_runtime_binding.py"]
        or start.get("process", {}).get("pid") != plan["owner"].get("process_id")
    ):
        raise mechanisms.MechanismError("retained B/source/worker configuration differs")
    terminal, terminal_ref = _read_lifecycle_record(
        directory, "terminal-cleanup-" + worker_id + ".json", key
    )
    if (
        terminal.get("schema") != "closer-native-correction-terminal-cleanup/v1"
        or terminal.get("binding") != binding(manifest)
        or terminal.get("admission") != context
        or terminal.get("worker") != fields
        or terminal.get("worker_id") != worker_id
        or terminal.get("handoff_sha256") != handoff_sha
        or terminal.get("native_b_start") != ref
        or terminal.get("process") != start["process"]
        or terminal.get("argv_sha256") != start.get("argv_sha256")
        or terminal.get("producer_sha256") != manifest["modules"]["closer_runtime_binding.py"]
        or terminal.get("coordinator_group_terminal") is not True
        or any(
            terminal.get(name) is not False
            for name in (
                "provider_completion_asserted",
                "domain_completed",
                "acceptance_authorized",
                "execution_authorized",
                "source_closure_authorized",
                "remote_execution_attested",
            )
        )
    ):
        raise mechanisms.MechanismError("retained source terminal-only cleanup differs")
    campaign_source._completion_cleanup(original_cfg, terminal, "unknown")
    result = ctl.IndexController._physical_state(
        fields["result"], "original native worker result redirected"
    )
    detail = json.loads(result["detail"])
    if (
        result.get("worker_id") != worker_id
        or result.get("target") != fields["target"]
        or result.get("mechanism") != mechanisms.NATIVE
        or result.get("native_admission") != context
        or result.get("outcome") not in {"unknown", "ok", "scoped_continuation"}
        or workers[0].get("outcome") != result.get("outcome")
        or detail.get("native_terminal_cleanup") != terminal_ref
        or detail.get("native_execution") != terminal.get("native_execution")
    ):
        raise mechanisms.MechanismError("original native worker result/cleanup join differs")
    links = terminal.get("operations")
    if type(links) is not list or len(links) > 128:
        raise mechanisms.MechanismError("retained cleanup operation inventory unavailable")
    matched = [
        row for row in links if row.get("plan_name") == "native-plan-" + plan["phase_key"] + ".json"
    ]
    if len(matched) != 1:
        raise mechanisms.MechanismError("retained cleanup did not observe this original operation")
    link = matched[0]
    if (
        link.get("plan_sha256") != ctl.digest(plan)
        or link.get("origin_sha256") != ctl.digest(origin)
        or link.get("operation_id") != plan["owner"].get("operation_id")
        or link.get("operation_sha256") != plan["owner"].get("operation_sha256")
        or link.get("provider_completion_asserted") is not False
    ):
        raise mechanisms.MechanismError("retained cleanup original operation join differs")
    return {
        "start": start,
        "terminal": terminal,
        "terminal_ref": terminal_ref,
        "original_config": original_cfg,
        "operation_link": link,
    }


def _require_original_coding_issuance(retained, reference):
    """A partial constructor WAL/row is never a returned source continuation."""
    result = retained.get("result") if type(retained) is dict else None
    issued = result.get("coding_continuation") if type(result) is dict else None
    if (
        retained.get("status") != "completed"
        or type(issued) is not dict
        or set(issued) != {"status", "reference", "execution_authorized", "retry_authorized"}
        or issued.get("status") != "awaiting_original_terminal"
        or issued.get("reference") != reference
        or issued.get("execution_authorized") is not False
        or issued.get("retry_authorized") is not False
    ):
        raise mechanisms.MechanismError("original coding reference was not completely issued")


def _coding_original_history_knowledge(manifest, campaign_doc, config, reference):
    """Shared original custody reader; caller supplies only source-authenticated context."""
    import closer_coding_continuation as continuation
    import closer_correction_controller as ctl
    import closer_correction_enrollment_terminal as enrollment
    import closer_queue as queue

    ref = continuation.validate_reference(reference)
    with contextlib.closing(ctl._Custody(config)) as custody:
        record = continuation._read_protected(custody, ref)
        marker_ref = record["enrollment_terminal_ref"]
        marker = custody.read(marker_ref["name"])
        if (
            enrollment._ref(marker) != marker_ref
            or marker["admission_sha256"] != record["admission_sha256"]
            or marker["native_b_start"] != record["native_b_start"]
            or marker["operation_json"] != record["operation_json"]
            or marker["operation_sha256"] != record["operation_sha256"]
            or marker["controller_config"] != config.source_binding()
        ):
            raise mechanisms.MechanismError("original continuation/enrollment identity differs")
        context = json.loads(marker["context_identity_json"])
        pipeline = ctl.IndexController._physical_state(
            context["state_path"], "original coding pipeline redirected"
        )
        bucket = pipeline["frontiers"][marker["finite_frontier"]["frontier_id"]]
        row = bucket["items"][marker["target"]]
        expected_stage = {
            "pipeline_state_path": context["state_path"],
            "pipeline_run_id": bucket["run_id"],
            "work_key": row["work_key"],
            "identity": row["identity"],
            "revision": row["revision"],
            "action_state_path": str(
                Path(context["state_path"]).parent
                / (Path(context["state_path"]).stem + ".actions")
                / (queue._digest(marker["target"]) + ".json")
            ),
        }
        operation = json.loads(record["operation_json"])
        boundary = enrollment.validate_queue_boundary(
            manifest_path=record["manifest_path"],
            state_path=marker["queue_path"],
            target=marker["target"],
            finite_frontier=marker["finite_frontier"],
            proof_ref=marker_ref,
            expected_stage=expected_stage,
            expected_operation_id=operation["id"],
        )
        if boundary is None:
            raise mechanisms.MechanismError("original enrollment knowledge no longer authentic")
        admission = custody.read("admission-" + record["admission_sha256"] + ".json")
        intent = custody.read(
            "native-intent-" + admission["own_instruction"]["phase_key"] + ".json"
        )
        if (
            ctl.digest(admission) != record["admission_sha256"]
            or intent["phase"] != "instruction_publication"
            or ctl.digest(intent) != marker["instruction_effect"]["intent_sha256"]
            or intent["owner"]["operation_sha256"] != record["operation_sha256"]
        ):
            raise mechanisms.MechanismError("original instruction intent/admission differs")
        history = _retained_enrollment_cleanup(manifest, campaign_doc, marker, marker_ref)
        link = history["operation_link"]
        old_queue = ctl.IndexController._physical_state(
            marker["queue_path"], "original coding queue redirected"
        )
        retained = old_queue["frontiers"][marker["finite_frontier"]["frontier_id"]]["items"][
            marker["target"]
        ]["operation"]
        _require_original_coding_issuance(retained, ref)
        if (
            hashlib.sha256(queue._canonical_bytes(retained)).hexdigest()
            != link["retained_operation_sha256"]
            or hashlib.sha256(queue._canonical_bytes(row)).hexdigest()
            != link["provider_history"]["row_sha256"]
            or link["provider_history"]["source_record_ref"] != marker["source_record_ref"]
        ):
            raise mechanisms.MechanismError("original signed queue/provider history changed")
        return {
            "schema": "closer-coding-original-history/v1",
            "continuation_reference": ref,
            **history,
            "enrollment_boundary": boundary,
            "instruction_intent": intent,
            "admission_sha256": record["admission_sha256"],
        }


_CODING_SELECTOR_SCHEMA = "closer-native-coding-worker-registration/v1"
_CODING_SELECTOR_FIELDS = frozenset(
    {
        "schema",
        "worker_id",
        "target",
        "claim_target",
        "nonce",
        "reference",
        "source_target",
        "coding_target",
        "queue_state",
        "finite_frontier",
        "request_timeout_seconds",
        "purpose",
    }
)


def _coding_purpose_projection(config, record):
    """Derived original subject only; this data is never execution admission."""
    import closer_coding_continuation as continuation
    import closer_coding_worker as worker

    continuation.validate_record(record)
    request = json.loads(record["request_json"])
    incumbent = request["incumbent"]
    if incumbent.get("kind") == "no_incumbent":
        raise mechanisms.MechanismError("coding requires original assigned native incumbent")
    return worker.validate_purpose(
        {
            "schema": worker.PURPOSE_SCHEMA,
            "consumer_id": record["consumer_id"],
            "target": config.repository + "#" + incumbent["issue"]["number"],
            "which": {
                "kind": "issue",
                **{key: incumbent["issue"][key] for key in ("number", "node_id")},
            },
            "selected": record["admitted_selector"],
            "expected": {"key": record["effect_key"], "request_sha256": record["request_sha256"]},
        }
    )


def _coding_dispatch_binding(config, projection, purpose, reference):
    import closer_coding_continuation as continuation
    import closer_correction_controller as ctl

    ref = continuation.validate_reference(reference)
    return {
        "pr_target": purpose["target"],
        "source_targets": [],
        "correction_controller_sha256": ctl.digest(config.source_binding()),
        "correction_installation_sha256": projection["installation_sha256"],
        "coding_worker_purpose_sha256": ctl.digest(purpose),
        "coding_continuation_sha256": ref["sha256"],
    }


def _validate_coding_selector(value, campaign_doc, config, record, projection):
    """Exact protected-source correspondence, not a caller JSON permit."""
    import closer_coding_continuation as continuation
    import closer_correction_controller as ctl
    import closer_frontier_policy as frontier
    from closer_campaign import ROUND_UNIT

    if type(value) is not dict or set(value) != _CODING_SELECTOR_FIELDS:
        raise mechanisms.MechanismError("exact coding worker registration required")
    ref = continuation._reference(record)
    purpose = _coding_purpose_projection(config, record)
    if (
        value["schema"] != _CODING_SELECTOR_SCHEMA
        or type(value["worker_id"]) is not str
        or not re.fullmatch(r"[a-f0-9]{12}", value["worker_id"])
        or type(value["nonce"]) is not str
        or not re.fullmatch(r"[a-f0-9]{32}", value["nonce"])
        or continuation._canonical(value["reference"]) != continuation._canonical(ref)
        or continuation._canonical(value["purpose"]) != continuation._canonical(purpose)
        or value["target"] != ROUND_UNIT
        or value["claim_target"] != campaign_doc["campaign_id"] + ":coding:" + ref["stable_key"]
        or value["coding_target"] != purpose["target"]
        or value["queue_state"]
        != str(Path(config.state_path + ".coding") / (ref["stable_key"] + ".json"))
        or type(value["source_target"]) is not str
        or value["source_target"]
        not in json.loads(record["operation_json"])["finite_frontier"]["targets"]
        or type(value["request_timeout_seconds"]) not in (int, float)
        or not 0 < value["request_timeout_seconds"] <= 120
    ):
        raise mechanisms.MechanismError("coding worker original source mapping differs")
    descriptor = frontier.validate_frontier(value["finite_frontier"], targets=[purpose["target"]])
    revision = ctl.digest(
        {"reference": ref, "worker_id": value["worker_id"], "nonce": value["nonce"]}
    )
    binding_value = _coding_dispatch_binding(config, projection, purpose, ref)
    if (
        descriptor["owner"] != campaign_doc["owner"]
        or descriptor["frontier_id"] != "coding-" + value["worker_id"]
        or descriptor["bindings"][purpose["target"]]
        != {
            "revision": revision,
            "action_id": frontier.action_identity(
                purpose["target"], revision, "correction_coding", binding_value
            ),
        }
    ):
        raise mechanisms.MechanismError("coding worker frozen action identity differs")
    return value


def accepted_coding_worker_purpose(binding_value):
    """Existing actual WORKER root only; no manifest JSON selects this authority."""
    import copy

    import closer_coding_worker as worker
    import closer_source_installation as installed
    from closer_campaign import load_manifest
    from closer_correction_source_record import SourceRecordUnavailable

    frame = inspect.currentframe().f_back
    try:
        if frame.f_globals is not worker.__dict__:
            raise SourceRecordUnavailable("actual coding WORKER purpose reader required")
        if frame.f_code is worker._issue_root.__code__:
            installation = frame.f_locals.get("installation")
        elif frame.f_code is worker._Root.check.__code__:
            root = frame.f_locals.get("self")
            installation = root.installation if type(root) is worker._Root else None
        else:
            raise SourceRecordUnavailable("actual coding WORKER purpose reader required")
        if (
            type(installation) is not installed.SourceInstallation
            or installation._binding is not binding_value
        ):
            raise SourceRecordUnavailable("actual coding installation identity unavailable")
        with installed.accepted_coding_validation_read(
            installation, inspect.currentframe()
        ) as qualified_read:
            qualified_read.check_inputs(binding_value, qualified_read.manifest)
            context = installation.admission
            selector = context.get("coding_continuation")
            if type(selector) is not dict:
                raise SourceRecordUnavailable("accepted signed coding purpose unavailable")
            manifest = load_manifest(installation.manifest_path)
            validate_admission(manifest, context, for_effects=True)
            start = source_b_start(installation)
            if (
                start is None
                or Path(start["path"]).stem != "worker-b-start-" + selector["worker_id"]
            ):
                raise SourceRecordUnavailable("accepted coding worker B-start differs")
            return {
                "schema": "closer-accepted-coding-worker-purpose/v1",
                "continuation_reference": copy.deepcopy(selector["reference"]),
                "purpose": copy.deepcopy(selector["purpose"]),
                "worker_id": selector["worker_id"],
                "admission_sha256": _hash(
                    contract(manifest)[3] / context["nonce"] / "admission.json"
                ),
                "native_b_start": start,
            }
    finally:
        del frame


def _coding_current_qualified_value(manifest, context):
    """Borrow only the freshly qualified value in this exact source snapshot call."""
    import closer_runtime_binding as runtime
    import closer_source_installation as installed

    frame = inspect.currentframe().f_back
    try:
        # Fixed chain only, never walk arbitrary frames or retain cached truth:
        # this helper <- coding record <- native validation <- source snapshot.
        if (
            frame is None
            or frame.f_code is not _coding_record_from_context.__code__
            or frame.f_globals is not globals()
        ):
            return None
        validation = frame.f_back
        snapshot = validation.f_back if validation is not None else None
        if (
            validation is None
            or validation.f_code is not validate_admission.__code__
            or validation.f_globals is not globals()
            or validation.f_locals.get("context") is not context
            or validation.f_locals.get("manifest") is not manifest
        ):
            return None
        if (
            snapshot is None
            or snapshot.f_code is not installed._snapshot_current.__code__
            or snapshot.f_globals is not installed.__dict__
            or snapshot.f_locals.get("context") is not context
            or snapshot.f_locals.get("manifest") is not manifest
        ):
            return _accepted_frame_qualified_value(manifest, context, validation)
        value = snapshot.f_locals.get("value")
        binding_value = snapshot.f_locals.get("binding")
        origin = runtime._factory_manifest_origin(binding_value)
        if (
            type(value) is not dict
            or origin is None
            or value.get("manifest_path") != origin.path
            or value.get("manifest_sha256") != origin.sha256
            or origin.path != context["runtime_manifest"]
            or origin.sha256 != context["runtime_manifest_sha256"]
        ):
            raise mechanisms.MechanismError("current coding qualified snapshot identity differs")
        token = snapshot.f_locals.get("_native_scope")
        if token is not None:
            if type(token) is not installed._NativeRuntimeRead:
                raise mechanisms.MechanismError("current coding snapshot token differs")
            token.check(manifest)
        return value, manifest
    finally:
        del frame


def _accepted_frame_qualified_value(manifest, context, validation):
    """Only five fixed native paths may reuse immutable accepted-frame work."""
    import closer_source_installation as installed

    paths = (
        (accepted_coding_worker_purpose,),
        (source_b_start, accepted_coding_worker_purpose),
        (_lifecycle_authority, source_b_start, accepted_coding_worker_purpose),
        (result_admission_config, source_b_start, accepted_coding_worker_purpose),
        (admission_config, result_admission_config, source_b_start, accepted_coding_worker_purpose),
    )
    matched = None
    for path in paths:
        current, chain = validation.f_back, []
        for function in path:
            if (
                current is None
                or current.f_code is not function.__code__
                or current.f_globals is not globals()
            ):
                break
            chain.append(current)
            current = current.f_back
        else:
            matched = chain
            break
    if matched is None:
        return None
    accepted = matched[-1]
    read = accepted.f_locals.get("qualified_read")
    if read is None:
        return None  # Initial full snapshot precedes the active batch local.
    if type(read) is not installed._AcceptedCodingRead:
        raise mechanisms.MechanismError("accepted read identity differs")
    installation = accepted.f_locals.get("installation")
    if installation is not read.installation:
        raise mechanisms.MechanismError("accepted installation alias differs")
    for frame in matched[:-1]:
        if frame.f_code is source_b_start.__code__:
            okay = (
                frame.f_locals.get("installation") is installation
                and frame.f_locals.get("context") is context
                and frame.f_locals.get("manifest") == manifest
            )
        elif frame.f_code is _lifecycle_authority.__code__:
            cfg = frame.f_locals.get("cfg")
            okay = (
                type(cfg) is dict
                and cfg.get("native_admission") is context
                and frame.f_locals.get("manifest") is manifest
            )
        else:
            okay = (
                frame.f_locals.get("context") is context
                and frame.f_locals.get("manifest") is manifest
            )
        if not okay:
            raise mechanisms.MechanismError("accepted native argument aliases differ")
    if len(matched) == 4 and matched[0].f_locals.get("cfg") is not matched[1].f_locals.get("cfg"):
        raise mechanisms.MechanismError("accepted configuration aliases differ")
    if context != read.original_value["admission"]:
        raise mechanisms.MechanismError("accepted admission copy differs")
    read.check_inputs(installation._binding, manifest)
    value, pinned = installed._qualified_policy(installation._binding, _accepted_read=read)
    read.check_inputs(installation._binding, manifest)
    return value, pinned


def _coding_record_from_context(manifest, context):
    """Read a signed purpose's protected original record under unchanged config."""
    import closer_coding_continuation as continuation
    import closer_correction_controller as ctl
    import closer_runtime_binding as runtime
    import closer_source_installation as installed

    selector = context.get("coding_continuation")
    if type(selector) is not dict:
        raise mechanisms.MechanismError("source coding purpose unavailable")
    current = _coding_current_qualified_value(manifest, context)
    if current is None:
        # Control-only callers outside a source snapshot retain full qualification.
        binding_value = runtime.binding_from_manifest(context["runtime_manifest"])
        if binding_value is None:
            raise mechanisms.MechanismError("coding purpose factory qualification unavailable")
        value, pinned = installed._qualified_policy(binding_value)
    else:
        value, pinned = current
    if pinned != manifest or value["manifest_sha256"] != context["runtime_manifest_sha256"]:
        raise mechanisms.MechanismError("coding purpose current source binding differs")
    config = ctl.ControllerConfig(**value["config"])
    with contextlib.closing(ctl._Custody(config)) as custody:
        record = continuation._read_protected(custody, selector["reference"])
    document = ctl.IndexController._physical_state(config.state_path, "coding campaign redirected")
    registry = continuation.validate_registry(document.get(continuation.REGISTRY_FIELD))
    row = registry.get(record["stable_key"])
    if (
        row is None
        or row["reference"] != selector["reference"]
        or continuation._canonical(row["worker"]) != continuation._canonical(selector)
        or row["state"] not in {"admitted", "launch_intent", "submitted", "active", "terminal"}
        or context["campaign"] != _campaign_identity(Path(config.state_path), document)
        or context["nonce"] != selector["nonce"]
        or context["finite_frontier"] != selector["finite_frontier"]
        or context["request_timeout_seconds"] != selector["request_timeout_seconds"]
        or (document.get("worker_config") or {}).get("policy") != manifest.get("policy")
    ):
        raise mechanisms.MechanismError("coding admission registry/worker identity differs")
    if (
        record["controller_config_sha256"] != ctl.digest(config.source_binding())
        or record["manifest_path"] != value["manifest_path"]
        or record["manifest_sha256"] != value["manifest_sha256"]
        or record["source_closure_sha256"] != ctl.digest(value["source_closure"])
    ):
        raise mechanisms.MechanismError("coding protected original source binding differs")
    _validate_coding_selector(selector, document, config, record, value["policy_projection"])
    return value, record, document


def _submitted_coding_operation(retained):
    """Closed completion-only projection in the existing queue JSON domain."""
    if type(retained) is not dict or retained.get("status") != "completed":
        raise mechanisms.MechanismError("coding callback has no known completed operation")
    removed = {"result", "elapsed_seconds", "next_trigger", "completed_at", "invoked_at"}
    original = {k: v for k, v in retained.items() if k not in removed}
    original["status"] = "submitted"
    clocks = [
        retained.get(k)
        for k in ("submitted_at", "request_started_at", "invoked_at", "completed_at", "deadline")
    ]
    if (
        retained.get("action") != "correction_coding"
        or type(retained.get("attempts")) is not int
        or retained["attempts"] != 1
        or any(type(v) not in {int, float} or not math.isfinite(v) for v in clocks)
        or not clocks[0] <= clocks[1] <= clocks[2] <= clocks[3] < clocks[4]
        or any(
            retained.get(k) is not None and retained.get(k) is not False
            for k in ("provider_outcome_uncertain", "outcome_uncertain")
        )
        or retained.get("recovery_barrier") is not None
    ):
        raise mechanisms.MechanismError(
            "coding callback original finite window or uncertainty differs"
        )
    return original


def _coding_knowledge_barriers(*values):
    """Identity-strict uncertainty barriers over every selected queue envelope."""
    for value in values:
        if (
            type(value) is not dict
            or value.get("status") in {"unknown", "uncertain", "owned_unknown"}
            or value.get("recovery_barrier") is not None
            or any(
                value.get(k) is not None and value.get(k) is not False
                for k in ("provider_outcome_uncertain", "outcome_uncertain")
            )
        ):
            raise mechanisms.MechanismError("coding queue uncertainty barrier remains owned")


def _coding_queue_terminal(cfg, candidate):
    import closer_coding_worker as worker
    import closer_correction_controller as ctl
    import closer_queue as queue

    context = cfg["native_admission"]
    selector = context["coding_continuation"]
    frontier = selector["finite_frontier"]
    state = ctl.IndexController._physical_state(
        selector["queue_state"], "coding terminal queue redirected"
    )
    if set(state.get("frontiers", {})) != {frontier["frontier_id"]}:
        raise mechanisms.MechanismError("coding queue contains foreign frontier history")
    if state.get("items") not in (None, {}) or state.get("round") not in (None, {}):
        raise mechanisms.MechanismError("coding private queue legacy namespace not empty")
    bucket = state["frontiers"][frontier["frontier_id"]]
    _coding_knowledge_barriers(state, bucket, bucket.get("round"), candidate)
    if (
        bucket.get("policy") != frontier
        or bucket.get("round", {}).get("finite_frontier") != frontier
        or bucket.get("round", {}).get("status") != "finished"
        or queue._canonical_bytes(bucket["round"].get("receipt"))
        != queue._canonical_bytes(candidate)
        or set(bucket.get("items", {})) != {selector["coding_target"]}
    ):
        raise mechanisms.MechanismError("coding returned receipt differs from physical queue")
    row = bucket["items"][selector["coding_target"]]
    if row.get("operation_history") not in (None, []) or row.get("recovery_barrier") is not None:
        raise mechanisms.MechanismError("coding previous queue history remains owned")
    retained = row["operation"]
    original = _submitted_coding_operation(retained)
    result = retained.get("result")
    _coding_knowledge_barriers(row, retained, result)
    if type(result) is dict and set(result) != {
        "coding_terminal_reference",
        "actual",
        *worker._NO_AUTHORITY,
    }:
        import closer_coding_publication as publication

        publication.validate_native_coding_result(cfg, original, result)
    if (
        type(result) is not dict
        or not {"coding_terminal_reference", "actual", *worker._NO_AUTHORITY} <= set(result)
        or any(result[k] is not False for k in worker._NO_AUTHORITY)
        or original["finite_frontier"] != frontier
        or original["revision"] != frontier["bindings"][selector["coding_target"]]["revision"]
        or row.get("provider_outcome_uncertain") not in (None, False)
        or row.get("outcome_uncertain") not in (None, False)
    ):
        raise mechanisms.MechanismError("coding queue terminal result not source known")
    ref = worker.validate_terminal_reference(result["coding_terminal_reference"])
    return (
        original,
        ref,
        result["actual"],
        hashlib.sha256(queue._canonical_bytes(state)).hexdigest(),
    )


_CODING_HELD_STAGES = frozenset(
    {"worker_entry", "issue_root", "root_check", "preflight_original", "run_queue", "execute"}
)
_CODING_HELD_MESSAGES = {
    "actual admitted dispatcher WORKER root required": "admitted_root_required",
    "source coding backend required": "source_backend_required",
    "current native source installation unavailable": "source_installation_unavailable",
    "accepted coding consumer differs from static capability": "consumer_capability_changed",
    "source coding assignment/frontier differs": "assignment_frontier_changed",
    "actual accepted native worker start unavailable": "native_start_unavailable",
    "source worker root unavailable": "root_lifetime_unavailable",
    "original source worker root ended": "root_lifetime_ended",
    "current worker source installation changed": "source_installation_changed",
    "actual accepted native worker changed": "native_worker_changed",
    "accepted coding purpose changed": "accepted_purpose_changed",
    "source coding consumer unavailable": "consumer_unavailable",
    "coding consumer policy differs": "consumer_policy_changed",
    "coding profile capacity pools unavailable": "profile_pools_unavailable",
    "original protected coding admission changed": "original_admission_changed",
    "original coding consumer differs": "original_consumer_changed",
    "original coding correction identity differs": "original_identity_changed",
}
_CODING_HELD_CODES = frozenset(
    {*_CODING_HELD_MESSAGES.values(), "source_refusal", "os_error", "invalid_source_data"}
)


def coding_worker_held(error):
    """Project actual traceback code identities and fixed codes, never error text."""
    import closer_coding_worker as worker

    stages = (
        (worker._issue_root.__code__, "issue_root"),
        (worker._Root.check.__code__, "root_check"),
        (worker._preflight_original.__code__, "preflight_original"),
        (worker._run_queue.__code__, "run_queue"),
        (worker._execute.__code__, "execute"),
    )
    stage = "worker_entry"
    trace = error.__traceback__
    while trace is not None:
        frame = trace.tb_frame
        if frame.f_globals is worker.__dict__:
            for code_object, name in stages:
                if frame.f_code is code_object:
                    stage = name
        trace = trace.tb_next
    code = (
        "os_error"
        if isinstance(error, OSError)
        else (
            "invalid_source_data"
            if isinstance(error, (ValueError, TypeError, KeyError))
            else "source_refusal"
        )
    )
    if type(error) is worker.WorkerHeld:
        code = "source_refusal"
        if stage != "worker_entry" and len(error.args) == 1 and type(error.args[0]) is str:
            code = _CODING_HELD_MESSAGES.get(error.args[0], code)
    return {
        "schema": "closer-coding-worker-held/v1",
        "status": "held",
        "reason": "coding_worker_source_refused",
        "diagnostic": {"stage": stage, "code": code},
        "execution_authorized": False,
        "retry_authorized": False,
    }


def _coding_held_diagnostic(stdout):
    """Accept only the complete small typed source frame, not arbitrary child JSON."""
    fallback = {"stage": "coding_b_output", "code": "no_typed_held_frame"}
    if type(stdout) is not str or len(stdout.encode("utf-8")) > 4096:
        return fallback

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate diagnostic field")
            result[key] = value
        return result

    try:
        value = json.loads(stdout, object_pairs_hook=unique)
        if (
            type(value) is not dict
            or set(value)
            != {
                "schema",
                "status",
                "reason",
                "diagnostic",
                "execution_authorized",
                "retry_authorized",
            }
            or value["schema"] != "closer-coding-worker-held/v1"
            or value["status"] != "held"
            or value["reason"] != "coding_worker_source_refused"
            or value["execution_authorized"] is not False
            or value["retry_authorized"] is not False
            or type(value["diagnostic"]) is not dict
            or set(value["diagnostic"]) != {"stage", "code"}
            or value["diagnostic"]["stage"] not in _CODING_HELD_STAGES
            or value["diagnostic"]["code"] not in _CODING_HELD_CODES
        ):
            return fallback
        return dict(value["diagnostic"])
    except (ValueError, TypeError, KeyError, UnicodeError):
        return fallback


def coding_b_error_diagnostic(error, *, stage):
    """Fixed secondary categories; no paths, exception messages or user output."""
    if stage not in {"coding_b_observation", "coding_b_delivery"}:
        raise mechanisms.MechanismError("unsupported coding diagnostic stage")
    code = (
        "permission_denied"
        if isinstance(error, PermissionError)
        else (
            "file_missing"
            if isinstance(error, FileNotFoundError)
            else (
                "invalid_data"
                if isinstance(error, (ValueError, TypeError, KeyError))
                else "source_refusal"
            )
        )
    )
    return {"stage": stage, "code": code}


def coding_b_observation_summary(done, witness):
    """Necessary bounded projection; only the source producer below signs custody."""
    if (
        type(done.returncode) is not int
        or not -128 <= done.returncode <= 255
        or any(
            type(value) is not str or len(value.encode("utf-8")) > 32 * 1024 * 1024
            for value in (done.stdout, done.stderr)
        )
    ):
        raise mechanisms.MechanismError("bounded coding B observation required")
    return {
        "exit_code": done.returncode,
        "cleanup_terminal": witness.get("finished", {}).get("cleanup_terminal") is True,
        "diagnostic": _coding_held_diagnostic(done.stdout),
        "execution_authorized": False,
        "acceptance_authorized": False,
    }


def record_coding_b_observation(cfg, done, witness):
    """Separate signed transport diagnostic; never delivery, terminal or C authority."""
    import closer_runtime_binding as runtime

    caller = inspect.currentframe().f_back
    if (
        caller.f_code is not runtime._observed_correction_b.__code__
        or caller.f_globals is not runtime.__dict__
        or caller.f_locals.get("cfg") is not cfg
        or caller.f_locals.get("done") is not done
        or caller.f_locals.get("witness") is not witness
        or witness.get("finished", {}).get("cleanup_terminal") is not True
    ):
        raise mechanisms.MechanismError("actual source coding B observation required")
    summary = coding_b_observation_summary(done, witness)
    record = {
        "schema": "closer-native-coding-b-observation/v1",
        "worker_id": witness["worker_id"],
        "native_b_start": witness["start"],
        "worker_config_sha256": mechanisms.digest(cfg),
        "process": {
            key: witness["finished"][key] for key in ("pid", "process_group", "os_started")
        },
        **summary,
        "stdout_bytes": len(done.stdout.encode("utf-8")),
        "stderr_bytes": len(done.stderr.encode("utf-8")),
        "stdout_sha256": hashlib.sha256(done.stdout.encode("utf-8")).hexdigest(),
        "stderr_sha256": hashlib.sha256(done.stderr.encode("utf-8")).hexdigest(),
        "domain_completed": False,
        "global_stop_valid": False,
        "provider_completion_asserted": False,
    }
    reference = witness["retain"]("coding-b-observation-" + witness["worker_id"] + ".json", record)
    return {**summary, "reference": reference}


def record_coding_b_delivery(cfg, done, witness):
    """Retain actual original supervised B output before C, granting no execution."""
    import closer_runtime_binding as runtime

    caller = inspect.currentframe().f_back
    if (
        caller.f_code is not runtime._observed_correction_b.__code__
        or caller.f_globals is not runtime.__dict__
        or caller.f_locals.get("cfg") is not cfg
        or caller.f_locals.get("done") is not done
        or caller.f_locals.get("witness") is not witness
        or witness.get("finished", {}).get("cleanup_terminal") is not True
        or done.returncode not in {0, 78}
    ):
        raise mechanisms.MechanismError("actual source supervised coding B delivery required")
    manifest = witness["manifest"]
    context = validate_admission(manifest, cfg["native_admission"], for_effects=False)
    selector = context["coding_continuation"]
    candidate = json.loads(done.stdout)
    operation, terminal_ref, actual, queue_sha = _coding_queue_terminal(cfg, candidate)
    import closer_queue as queue

    record = {
        "schema": "closer-native-coding-b-delivery/v1",
        "binding": binding(manifest),
        "admission": context,
        "worker": witness["fields"],
        "worker_id": witness["worker_id"],
        "handoff_sha256": witness["handoff_sha"],
        "native_b_start": witness["start"],
        "process": {k: witness["finished"][k] for k in ("pid", "process_group", "os_started")},
        "worker_config_json": json.dumps(
            cfg, sort_keys=True, separators=(",", ":"), allow_nan=False
        ),
        "worker_config_sha256": mechanisms.digest(cfg),
        "terminal_reference": terminal_ref,
        "queue_sha256": queue_sha,
        "operation_sha256": hashlib.sha256(queue._canonical_bytes(operation)).hexdigest(),
        "actual_sha256": hashlib.sha256(queue._canonical_bytes(actual)).hexdigest(),
        "stdout_sha256": hashlib.sha256(done.stdout.encode("utf-8")).hexdigest(),
        "candidate": candidate,
        "coordinator_group_terminal": True,
        "domain_completed": False,
        "acceptance_authorized": False,
        "execution_authorized": False,
        "global_stop_valid": False,
        "provider_completion_asserted": False,
    }
    if selector["worker_id"] != witness["worker_id"]:
        raise mechanisms.MechanismError("coding delivery worker differs from actual B")
    return witness["retain"]("coding-b-delivery-" + selector["worker_id"] + ".json", record)


def _coding_delivery_knowledge(manifest, campaign_document, cfg, reference):
    """Read source-signed pre-C delivery; this is never a new WORKER installation."""
    import closer_correction_controller as ctl
    import closer_queue as queue

    directory, key, context, fields, worker_id, handoff_sha = _lifecycle_authority(manifest, cfg)
    selector = context["coding_continuation"]
    value, _protected, current = _coding_record_from_context(manifest, context)
    if mechanisms.digest(current) != mechanisms.digest(campaign_document):
        raise mechanisms.MechanismError("coding C campaign current selector differs")
    delivery, delivery_ref = _read_lifecycle_record(
        directory, "coding-b-delivery-" + worker_id + ".json", key
    )
    start, start_ref = _read_lifecycle_record(
        directory, "worker-b-start-" + worker_id + ".json", key
    )
    config = ctl.ControllerConfig(**value["config"])
    original, terminal_ref, actual, queue_sha = _coding_queue_terminal(
        cfg, delivery.get("candidate")
    )
    if (
        delivery.get("schema") != "closer-native-coding-b-delivery/v1"
        or delivery.get("binding") != binding(manifest)
        or delivery.get("admission") != context
        or delivery.get("worker") != fields
        or delivery.get("worker_id") != worker_id
        or delivery.get("handoff_sha256") != handoff_sha
        or delivery.get("native_b_start") != start_ref
        or delivery.get("process") != start.get("process")
        or delivery.get("worker_config_sha256") != mechanisms.digest(cfg)
        or json.loads(delivery.get("worker_config_json", "null")) != cfg
        or delivery.get("terminal_reference") != reference
        or terminal_ref != reference
        or delivery.get("queue_sha256") != queue_sha
        or delivery.get("operation_sha256")
        != hashlib.sha256(queue._canonical_bytes(original)).hexdigest()
        or delivery.get("actual_sha256")
        != hashlib.sha256(queue._canonical_bytes(actual)).hexdigest()
        or delivery.get("coordinator_group_terminal") is not True
        or any(
            delivery.get(k) is not False
            for k in (
                "domain_completed",
                "acceptance_authorized",
                "execution_authorized",
                "global_stop_valid",
                "provider_completion_asserted",
            )
        )
        or start.get("admission") != context
        or start.get("worker") != fields
        or start.get("worker_id") != worker_id
        or start.get("handoff_sha256") != handoff_sha
        or start.get("worker_config_sha256") != mechanisms.digest(cfg)
        or start.get("producer_sha256") != manifest["modules"]["closer_runtime_binding.py"]
        or _hash(Path(cfg["runtime_manifest"])) != context["runtime_manifest_sha256"]
    ):
        raise mechanisms.MechanismError("coding source B delivery and original queue differ")
    history = _coding_original_history_knowledge(manifest, current, config, selector["reference"])
    accepted = {
        "schema": "closer-accepted-coding-worker-purpose/v1",
        "continuation_reference": selector["reference"],
        "purpose": selector["purpose"],
        "worker_id": worker_id,
        "admission_sha256": _hash(directory / "admission.json"),
        "native_b_start": start_ref,
    }
    if (
        ctl.IndexController._physical_state(config.state_path, "coding C campaign redirected")
        != current
        or _coding_queue_terminal(cfg, delivery["candidate"])[3] != queue_sha
        or _read_lifecycle_record(directory, "coding-b-delivery-" + worker_id + ".json", key)[1]
        != delivery_ref
    ):
        raise mechanisms.MechanismError("coding C source knowledge changed during read")
    return {
        "schema": "closer-coding-terminal-reader-context/v1",
        "config": value["config"],
        "accepted_purpose": accepted,
        "original_history": history,
        "operation": original,
        "manifest_sha256": value["manifest_sha256"],
        "worker_config_sha256": mechanisms.digest(cfg),
    }


def coding_terminal_reader_context(manifest, campaign_document, cfg, reference):
    import closer_coding_worker as worker
    from closer_correction_source_record import SourceRecordUnavailable

    caller = inspect.currentframe().f_back
    if (
        caller.f_code is not worker.read_terminal_for_native_c.__code__
        or caller.f_globals is not worker.__dict__
    ):
        raise SourceRecordUnavailable("actual source coding C knowledge reader required")
    return _coding_delivery_knowledge(manifest, campaign_document, cfg, reference)


def coding_terminal_proof(manifest, campaign_document, cfg, reference):
    """Protected local coding result knowledge only; C cleanup is separate."""
    import closer_coding_worker as worker

    record = worker.read_terminal_for_native_c(manifest, campaign_document, cfg, reference)
    directory, key, _context, _fields, worker_id, _handoff = _lifecycle_authority(manifest, cfg)
    delivery = _read_lifecycle_record(directory, "coding-b-delivery-" + worker_id + ".json", key)[0]
    _operation, terminal_ref, actual, _queue_sha = _coding_queue_terminal(
        cfg, delivery["candidate"]
    )
    import closer_queue as queue

    if (
        terminal_ref != reference
        or record["result_sha256"] != hashlib.sha256(queue._canonical_bytes(actual)).hexdigest()
    ):
        raise mechanisms.MechanismError(
            "coding terminal actual result differs from protected receipt"
        )
    return record


def coding_terminal_diagnostic(error):
    """Fixed source codes only; never retain arbitrary exception text or types."""
    codes = {
        "actual source coding B/C completion producer required": "actual_b_c_lifetime_unavailable",
        "coding C did not reach known nonaccepting boundary": "actual_c_gate_unavailable",
        "coding B/C worker config changed": "worker_config_changed",
        "coding terminal actual result differs from protected receipt": "actual_result_changed",
        "coding source B delivery and original queue differ": "delivery_queue_join_changed",
        "coding returned receipt differs from physical queue": "queue_receipt_changed",
        "coding callback original finite window or uncertainty differs": "callback_window_or_uncertainty",
        "coding queue uncertainty barrier remains owned": "queue_uncertainty",
        "coding C source knowledge changed during read": "knowledge_changed_during_read",
        "cohort lacks exact actual A/B0/C0 cleanup": "exact_c_cleanup_unavailable",
    }
    return {
        "stage": "coding_terminal",
        "code": codes.get(str(error), "unclassified_source_refusal"),
    }


def read_coding_delivery(reference):
    """Separate C process reads only source-signed original B knowledge."""
    import closer_campaign as campaign_source
    import closer_correction_controller as ctl

    if type(reference) is not dict or set(reference) != {"path", "sha256"}:
        raise mechanisms.MechanismError("exact coding B delivery reference required")
    path = Path(reference["path"])
    raw = _bounded_bytes(path, 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
        raise mechanisms.MechanismError("coding B delivery physical bytes changed")
    # These fields only locate a manifest. No knowledge is accepted before its
    # native signature, exact source closure and lifecycle are checked below.
    untrusted = json.loads(raw)["record"]
    cfg = json.loads(untrusted["worker_config_json"])
    manifest = campaign_source.load_manifest(cfg["runtime_manifest"])
    directory, key, _context, _fields, worker_id, _handoff = _lifecycle_authority(manifest, cfg)
    signed, actual_ref = _read_lifecycle_record(
        directory, "coding-b-delivery-" + worker_id + ".json", key
    )
    if actual_ref != reference or signed != untrusted:
        raise mechanisms.MechanismError("coding C delivery belongs to another native worker")
    document = ctl.IndexController._physical_state(
        cfg["native_admission"]["campaign"]["state"], "coding C campaign redirected"
    )
    terminal = coding_terminal_proof(manifest, document, cfg, signed["terminal_reference"])
    return manifest, document, cfg, terminal


def bind_coding_terminal_receipt(hooks, done, cfg):
    """Actual pinned B-receipt adapter selects private C knowledge, never a round."""
    import closer_campaign as campaign_source
    import closer_runtime_binding as runtime

    caller = inspect.currentframe().f_back
    if (
        caller.f_code is not runtime.PinnedNativeHooks.set_terminal_receipt.__code__
        or caller.f_globals is not runtime.__dict__
        or caller.f_locals.get("self") is not hooks
        or caller.f_locals.get("done") is not done
        or caller.f_locals.get("cfg") is not cfg
        or type(hooks) is not runtime.PinnedNativeHooks
        or not hooks.bounded
        or not hooks._dependencies_match()
    ):
        raise mechanisms.MechanismError("actual pinned coding B receipt adapter required")
    manifest = campaign_source.load_manifest(cfg["runtime_manifest"])
    directory, key, _context, _fields, worker_id, _handoff = _lifecycle_authority(manifest, cfg)
    delivery, ref = _read_lifecycle_record(
        directory, "coding-b-delivery-" + worker_id + ".json", key
    )
    if (
        done.returncode not in {0, 78}
        or delivery["stdout_sha256"] != hashlib.sha256(done.stdout.encode("utf-8")).hexdigest()
    ):
        raise mechanisms.MechanismError("coding B output differs from actual source delivery")
    read_coding_delivery(ref)
    hooks.env["CLOSER_POSTRUN_CODING_DELIVERY"] = ref["path"]
    hooks.env["CLOSER_POSTRUN_CODING_DELIVERY_SHA256"] = ref["sha256"]
    return True


def finalize_coding_native(cfg, hooks, witness, execution):
    """Actual C cleanup closes this local coding worker; never a PR/global stop."""
    import closer_campaign as campaign_source
    import closer_coding_worker as worker
    import closer_runtime_binding as runtime

    caller = inspect.currentframe().f_back
    if (
        caller.f_code is not runtime.run_native_round.__code__
        or caller.f_globals is not runtime.__dict__
        or caller.f_locals.get("cfg") is not cfg
        or caller.f_locals.get("hooks") is not hooks
        or caller.f_locals.get("cleanup_witness") is not witness
        or caller.f_locals.get("postrun_completed") is not True
        or caller.f_locals.get("outcome") == "denied"
        or caller.f_locals.get("receipt_bound") is not True
        or caller.f_locals.get("runner") is not subprocess.run
        or type(hooks) is not runtime.PinnedNativeHooks
        or not hooks._dependencies_match()
        or witness.get("finished", {}).get("cleanup_terminal") is not True
        or caller.f_locals["execution_report"]() != execution
    ):
        raise mechanisms.MechanismError("actual source coding B/C completion producer required")
    c = hooks.postrun_result
    stderr = c.get("stderr", "")
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    if (
        c.get("code") != 65
        or c.get("dependencies_valid") is not True
        or stderr.strip()
        != "handoff-postrun: closer stop rejected; preserve ledger and resume actionable work"
    ):
        raise mechanisms.MechanismError("coding C did not reach known nonaccepting boundary")
    campaign_source._completion_cleanup(cfg, {"native_execution": execution}, "scoped_continuation")
    manifest, document, original_cfg, terminal = read_coding_delivery(witness["coding_delivery"])
    if original_cfg != cfg:
        raise mechanisms.MechanismError("coding B/C worker config changed")
    record = {
        "schema": "closer-native-coding-terminal-cleanup/v1",
        "binding": binding(manifest),
        "admission": cfg["native_admission"],
        "worker": cfg["native_worker"],
        "native_b_start": witness["start"],
        "delivery": witness["coding_delivery"],
        "terminal_reference": _coding_queue_terminal(
            cfg, json.loads(caller.f_locals["terminal_b"].stdout)
        )[1],
        "native_execution": execution,
        "coordinator_group_terminal": True,
        **{k: False for k in worker._NO_AUTHORITY},
        "execution_authorized": False,
        "provider_completion_asserted": False,
    }
    ref = witness["retain"]("coding-terminal-cleanup-" + witness["worker_id"] + ".json", record)
    proof = {
        "schema": "closer-native-coding-continuation/v1",
        "domain_outcome": "known",
        "coding_terminal_reference": record["terminal_reference"],
        "coding_terminal_cleanup": ref,
        "continuation_reference": cfg["coding_continuation"],
        "local_coding_result_sha256": terminal["result_sha256"],
        **{k: False for k in worker._NO_AUTHORITY},
        "execution_authorized": False,
        "provider_completion_asserted": False,
    }
    actual = _coding_queue_terminal(cfg, json.loads(caller.f_locals["terminal_b"].stdout))[2]
    return {"proof": proof, "actual": actual}


def _coding_stage_record(manifest, selector, name):
    root = contract(manifest)[3]
    directory = root / selector["nonce"]
    if directory.resolve() != directory or directory.parent != root:
        raise mechanisms.MechanismError("coding source stage custody redirected")
    return _read_lifecycle_record(directory, name, _existing_lifecycle_key(root))[0]


def _coding_launch_argv(launch):
    """JSON handoff and OS byte-argv hashes have distinct existing domains."""
    argv = launch.get("argv") if type(launch) is dict else None
    if (
        type(argv) is not list
        or not argv
        or any(type(arg) is not str or "\0" in arg for arg in argv)
        or launch.get("argv_sha256") != mechanisms.digest(argv)
        or launch.get("os_argv_sha256") != mechanisms._argv_digest(argv)
    ):
        raise mechanisms.MechanismError("coding launch exact argv digest domains differ")
    return argv


def coding_terminal_for_tick(scope, reference):
    """Actual tick only; ordinary reads cannot bypass a registry transition."""
    import closer_coding_continuation as continuation

    if type(scope) is not continuation._CodingScope:
        raise mechanisms.MechanismError("actual coding tick scope required for terminal read")
    scope.check()
    continuation.read_tick_selected(scope, reference)
    result = _coding_terminal_knowledge(scope.manifest, scope.document, reference)
    scope.check()
    return result


def _require_coding_sidecar_exact(result, detail, cfg, cleanup):
    """Native finite-float JSON identity, never Python numeric alias equality."""
    if mechanisms.digest(result.get("native_admission")) != mechanisms.digest(
        cfg["native_admission"]
    ) or mechanisms.digest(detail.get("native_execution")) != mechanisms.digest(
        cleanup["native_execution"]
    ):
        raise mechanisms.MechanismError("coding returned admission or execution bytes differ")


def _coding_terminal_knowledge(manifest, campaign_document, reference):
    """Knowledge reader valid inside exact mandatory post-CAS source proof too."""
    import closer_campaign as campaign_source
    import closer_coding_continuation as continuation
    import closer_coding_worker as worker
    import closer_correction_controller as ctl
    import closer_queue as queue

    ref = continuation.validate_reference(reference)
    row = continuation.validate_registry(campaign_document[continuation.REGISTRY_FIELD])[
        ref["stable_key"]
    ]
    selector = row["worker"]
    if row["state"] not in {"submitted", "active", "terminal"} or selector is None:
        raise mechanisms.MechanismError("coding original launch remains unobserved")
    root = contract(manifest)[3]
    directory = root / selector["nonce"]
    key = _existing_lifecycle_key(root)
    signed_delivery, delivery_ref = _read_lifecycle_record(
        directory, "coding-b-delivery-" + selector["worker_id"] + ".json", key
    )
    retained_manifest, document, cfg, terminal = read_coding_delivery(delivery_ref)
    if (
        retained_manifest != manifest
        or document != campaign_document
        or cfg["coding_continuation"] != ref
    ):
        raise mechanisms.MechanismError("coding terminal belongs to another current source tick")
    cleanup, cleanup_ref = _read_lifecycle_record(
        directory, "coding-terminal-cleanup-" + selector["worker_id"] + ".json", key
    )
    if (
        cleanup.get("schema") != "closer-native-coding-terminal-cleanup/v1"
        or cleanup.get("binding") != binding(manifest)
        or cleanup.get("admission") != cfg["native_admission"]
        or cleanup.get("worker") != cfg["native_worker"]
        or cleanup.get("native_b_start") != signed_delivery["native_b_start"]
        or cleanup.get("delivery") != delivery_ref
        or cleanup.get("terminal_reference") != signed_delivery["terminal_reference"]
        or cleanup.get("coordinator_group_terminal") is not True
        or any(
            cleanup.get(k) is not False
            for k in (*worker._NO_AUTHORITY, "execution_authorized", "provider_completion_asserted")
        )
    ):
        raise mechanisms.MechanismError("coding terminal actual cleanup record differs")
    campaign_source._completion_cleanup(cfg, cleanup, "scoped_continuation")
    result = ctl.IndexController._physical_state(
        cfg["native_worker"]["result"], "coding worker result redirected"
    )
    detail = json.loads(result["detail"])
    proof = detail.get("proof")
    _require_coding_sidecar_exact(result, detail, cfg, cleanup)
    if (
        result.get("worker_id") != selector["worker_id"]
        or result.get("target") != selector["target"]
        or result.get("mechanism") != mechanisms.NATIVE
        or result.get("outcome") != "scoped_continuation"
        or type(proof) is not dict
        or proof.get("schema") != "closer-native-coding-continuation/v1"
        or proof.get("domain_outcome") != "known"
        or proof.get("coding_terminal_cleanup") != cleanup_ref
        or proof.get("coding_terminal_reference") != signed_delivery["terminal_reference"]
        or proof.get("continuation_reference") != ref
        or proof.get("local_coding_result_sha256") != terminal["result_sha256"]
        or any(
            proof.get(k) is not False
            for k in (*worker._NO_AUTHORITY, "execution_authorized", "provider_completion_asserted")
        )
        or hashlib.sha256(queue._canonical_bytes(detail.get("actual"))).hexdigest()
        != terminal["result_sha256"]
    ):
        raise mechanisms.MechanismError("coding terminal sidecar does not bind actual known B/C")
    if (
        mechanisms.digest(
            ctl.IndexController._physical_state(
                cfg["native_worker"]["result"], "coding result changed"
            )
        )
        != mechanisms.digest(result)
        or _read_lifecycle_record(
            directory, "coding-terminal-cleanup-" + selector["worker_id"] + ".json", key
        )[1]
        != cleanup_ref
    ):
        raise mechanisms.MechanismError("coding terminal changed during current source read")
    return {"worker": selector, "terminal": cleanup_ref, "proof": proof}


def validate_coding_registry_transition(scope, old_document, new_document):
    """Authenticate source artifacts for one existing-row CAS; no status is proof."""
    import closer_coding_continuation as continuation
    import closer_correction_controller as ctl

    # This exact read is mandatory before the helper may adopt new expected
    # bytes. It validates actual fixed frame/TLS/locks and post-CAS disk identity.
    observed = continuation._transition_snapshot(scope)
    ref = observed["reference"]
    continuation._registry_delta(old_document, new_document, ref)
    before = old_document[continuation.REGISTRY_FIELD][ref["stable_key"]]
    after = new_document[continuation.REGISTRY_FIELD][ref["stable_key"]]
    selector = after["worker"]
    root = contract(scope.manifest)[3]
    path = root / selector["nonce"] / "admission.json"
    signed = _read(path)
    context = signed["record"]["context"]
    if (
        signed["record"].get("binding") != binding(scope.manifest)
        or not hmac.compare_digest(
            signed["signature"],
            mechanisms._signature(signed["record"], _existing_lifecycle_key(root)),
        )
        or context.get("coding_continuation") != selector
    ):
        raise mechanisms.MechanismError("coding transition lacks exact signed native admission")
    validate_admission(scope.manifest, context, for_effects=after["state"] not in {"terminal"})
    config = ctl.ControllerConfig(
        **scope.manifest["policy"]["correction_controller_installation"]["config"]
    )
    with contextlib.closing(ctl._Custody(config)) as custody:
        intent = custody.read("coding-admission-intent-" + ref["stable_key"] + ".json")
        if (
            set(intent) != {"schema", "reference", "worker_json", "manifest_sha256"}
            or intent["schema"] != "closer-coding-admission-intent/v1"
            or intent["reference"] != ref
            or intent["manifest_sha256"] != scope.manifest_sha256
            or json.dumps(selector, sort_keys=True, separators=(",", ":"), allow_nan=False)
            != intent["worker_json"]
        ):
            raise mechanisms.MechanismError("coding source admission intent differs")
    if after["state"] == "admitted":
        if before["state"] != "awaiting_original_terminal":
            raise mechanisms.MechanismError("coding admission cannot renew existing worker")
        _coding_original_history_knowledge(scope.manifest, new_document, config, ref)
    elif after["state"] == "launch_intent":
        launch = _coding_stage_record(
            scope.manifest, selector, "coding-launch-intent-" + selector["worker_id"] + ".json"
        )
        _coding_launch_argv(launch)
        if (
            launch.get("schema") != "closer-native-coding-launch-intent/v1"
            or launch.get("worker") != selector
            or launch.get("reference") != ref
            or launch.get("admission_sha256") != _hash(path)
        ):
            raise mechanisms.MechanismError("coding launch lacks source create-only intent")
        handoff = _coding_stage_record(
            scope.manifest, selector, "worker-" + selector["worker_id"] + ".json"
        )
        if handoff.get("admission") != context or handoff.get("argv_sha256") != launch.get(
            "argv_sha256"
        ):
            raise mechanisms.MechanismError("coding launch handoff differs from original intent")
    elif after["state"] in {"submitted", "active"}:
        launch = _coding_stage_record(
            scope.manifest, selector, "coding-launch-intent-" + selector["worker_id"] + ".json"
        )
        actual = _coding_stage_record(
            scope.manifest, selector, "coding-launch-observation-" + selector["worker_id"] + ".json"
        )
        _coding_launch_argv(launch)
        if (
            actual.get("schema") != "closer-native-coding-launch-observation/v1"
            or actual.get("worker") != selector
            or actual.get("reference") != ref
            or actual.get("intent_sha256") != mechanisms.digest(launch)
            or type(actual.get("process", {}).get("pid")) is not int
            or actual["process"]["pid"] <= 1
            or actual["process"].get("argv_sha256") != launch.get("os_argv_sha256")
            or type(actual["process"].get("started")) is not str
            or not actual["process"]["started"]
            or actual["process"].get("source") != "os-byte-argv"
        ):
            raise mechanisms.MechanismError(
                "coding submission actual process observation unavailable"
            )
    elif after["state"] == "terminal":
        # Mandatory transition snapshot above owns post-CAS provenance; this
        # reader never checks a normal scope or adopts replacement expected data.
        _coding_terminal_knowledge(scope.manifest, new_document, ref)
    else:
        raise mechanisms.MechanismError("unsupported coding source transition")
    if (
        continuation._canonical(scope.campaign._read_unlocked())
        != continuation._canonical(new_document)
        or observed["new_document_sha256"]
        != hashlib.sha256(continuation._canonical(new_document)).hexdigest()
    ):
        raise mechanisms.MechanismError("coding transition source state changed during proof")


def _coding_tick_selector(scope, reference, expected_state):
    """Resolve only a protected row under the actual finite tick lock lifetime."""
    import closer_coding_continuation as continuation

    if type(scope) is not continuation._CodingScope:
        raise mechanisms.MechanismError("actual coding tick scope required before launch")
    scope.check()
    ref = continuation.validate_reference(reference)
    continuation.read_tick_selected(scope, ref)
    row = continuation.validate_registry(scope.document[continuation.REGISTRY_FIELD])[
        ref["stable_key"]
    ]
    if row["reference"] != ref or row["state"] != expected_state:
        raise mechanisms.MechanismError("coding launch phase already owned; no resubmission")
    root = contract(scope.manifest)[3]
    context = _CURRENT_ADMISSION.get(str(root), {}).get("context")
    if (
        type(context) is not dict
        or context.get("coding_continuation") != row["worker"]
        or context.get("nonce") != scope.nonce
    ):
        raise mechanisms.MechanismError("current source coding admission differs")
    validate_admission(scope.manifest, context, for_effects=True)
    scope.check()
    return ref, row["worker"], context


def _advance_coding_registry(scope, reference, state, *, worker=None):
    """Single source CAS; native artifact validator adopts its exact readback."""
    import closer_coding_continuation as continuation

    if type(scope) is not continuation._CodingScope:
        raise mechanisms.MechanismError("actual coding tick scope required before CAS")
    scope.check()
    ref = continuation.validate_reference(reference)
    old = scope.document
    updated = json.loads(continuation._canonical(old))
    row = updated[continuation.REGISTRY_FIELD][ref["stable_key"]]
    row["state"] = state
    row["revision"] += 1
    if worker is not None:
        row["worker"] = worker
    updated["revision"] += 1
    # Reject resets, changed workers and ordinary target/worker mutations BEFORE
    # writing; the context authenticates signed native proofs again after CAS.
    continuation._registry_delta(old, updated, ref)
    with continuation.registry_transition(scope, ref):
        scope.campaign_lock.readback(old)
        scope.campaign._write_unlocked(updated)
        scope.campaign_lock.readback(updated)
    return scope.document


def prepare_coding_launch(scope, reference):
    """Publish exact source argv and handoff once, then seal launch intent by CAS.

    No process is started here. A crash after either create owns this intent;
    subsequent ticks refuse rather than rebuilding admission or argv.
    """
    import closer_campaign as campaign_source

    ref, selector, context = _coding_tick_selector(scope, reference, "admitted")
    manifest, document = scope.manifest, scope.document
    root = contract(manifest)[3]
    directory = root / selector["nonce"]
    path = directory / ("coding-launch-intent-" + selector["worker_id"] + ".json")
    handoff = directory / ("worker-" + selector["worker_id"] + ".json")
    if path.exists() or path.is_symlink() or handoff.exists() or handoff.is_symlink():
        raise mechanisms.MechanismError("coding launch intent already owned; never repeat")
    argv = [
        str(contract(manifest)[2]),
        manifest["module_path"],
        "worker",
        "--state",
        str(scope.campaign.path),
        "--target",
        selector["target"],
        "--owner",
        document["owner"],
        "--generation",
        str(document.get("generation", 1)),
        "--runtime-manifest",
        scope.manifest_path,
        "--mechanism",
        mechanisms.NATIVE,
        "--result",
        str(campaign_source.result_path(scope.campaign.path, selector["worker_id"])),
    ]
    launch = {
        "schema": "closer-native-coding-launch-intent/v1",
        "worker": selector,
        "reference": ref,
        "admission_sha256": _hash(directory / "admission.json"),
        "argv": argv,
        "argv_sha256": mechanisms.digest(argv),
        "os_argv_sha256": mechanisms._argv_digest(argv),
    }
    _coding_launch_argv(launch)
    scope.check()
    validate_admission(manifest, context, for_effects=True)
    _immutable(
        path,
        json.dumps(
            {
                "record": launch,
                "signature": mechanisms._signature(launch, _existing_lifecycle_key(root)),
            },
            sort_keys=True,
            allow_nan=False,
        ).encode(),
    )
    authorize_worker(manifest, argv)
    _advance_coding_registry(scope, ref, "launch_intent")
    return launch


def launch_coding_worker(scope, reference):
    """One actual existing worker launch; uncertain observation never resubmits."""
    import claims
    import closer_campaign as campaign_source

    ref, selector, context = _coding_tick_selector(scope, reference, "launch_intent")
    root = contract(scope.manifest)[3]
    directory = root / selector["nonce"]
    launch = _coding_stage_record(
        scope.manifest, selector, "coding-launch-intent-" + selector["worker_id"] + ".json"
    )
    argv = _coding_launch_argv(launch)
    output = directory / ("coding-launch-observation-" + selector["worker_id"] + ".json")
    if output.exists() or output.is_symlink():
        raise mechanisms.MechanismError("coding launch observation already owned; never repeat")
    # The only caller is the source selector immediately after its own fresh
    # prepare/CAS. A later tick observing launch_intent cannot invoke Popen.
    caller = inspect.currentframe().f_back
    if (
        caller.f_code is not run_coding_if_selected.__code__
        or caller.f_globals is not globals()
        or caller.f_locals.get("scope") is not scope
        or caller.f_locals.get("_prepared") is not True
        or scope.tick.f_locals.get("launcher") is not campaign_source.default_launcher
    ):
        raise mechanisms.MechanismError("fresh source coding launch producer required")
    claim = claims.holder(selector["claim_target"])
    if not claim or claim.get("agent") != campaign_source.AGENT:
        raise mechanisms.MechanismError("coding launch exact claim unavailable")
    scope.check()
    validate_admission(scope.manifest, context, for_effects=True)
    pid = campaign_source.default_launcher(
        argv,
        scope.campaign.path.with_name(
            scope.campaign.path.name + "." + selector["worker_id"] + ".log"
        ),
    )
    # Existing OS-byte-argv observer uses its existing bounded metadata calls;
    # no queue request has started and no admission/request clock is renewed.
    process = mechanisms._identity(pid, argv)
    scope.check()
    actual = {
        "schema": "closer-native-coding-launch-observation/v1",
        "worker": selector,
        "reference": ref,
        "intent_sha256": mechanisms.digest(launch),
        "process": process,
    }
    _immutable(
        output,
        json.dumps(
            {
                "record": actual,
                "signature": mechanisms._signature(actual, _existing_lifecycle_key(root)),
            },
            sort_keys=True,
            allow_nan=False,
        ).encode(),
    )
    _advance_coding_registry(scope, ref, "submitted")
    claims.update_metadata(selector["claim_target"], campaign_source.AGENT, pid=pid)
    return {
        "kind": "coding_launched",
        "worker_id": selector["worker_id"],
        "pid": pid,
        "execution_authorized": False,
        "retry_authorized": False,
    }


def run_coding_if_selected(campaign, document, manifest, manifest_path, pending):
    """Existing native tick alternative; ordinary closed history precedes coding."""
    import claims
    import closer_campaign as campaign_source
    import closer_coding_continuation as continuation

    capability = (manifest.get("policy") or {}).get(continuation.POLICY_FIELD)
    if capability is None:
        return None
    continuation.validate_capability(capability)
    registry = continuation.validate_registry(document.get(continuation.REGISTRY_FIELD, {}))
    if not registry:
        return None
    if pending.get("kind") != "reconcile":
        return None  # Never intercept ordinary launch, hold, retirement or release.
    selected = [row for row in registry.values() if row["state"] != "terminal"]
    if len(selected) != 1:
        return {
            "kind": "held",
            "reason": "coding_selection_not_unique",
            "execution_authorized": False,
            "retry_authorized": False,
        }
    row = selected[0]
    if row["state"] != "awaiting_original_terminal":
        if row["state"] in {"submitted", "active"}:
            try:
                with continuation._coding_scope(
                    campaign, document, manifest, manifest_path
                ) as scope:
                    coding_terminal_for_tick(scope, row["reference"])
                    _advance_coding_registry(scope, row["reference"], "terminal")
                    return {
                        "kind": "coding_local_terminal",
                        "global_stop_valid": False,
                        "acceptance_authorized": False,
                        "execution_authorized": False,
                        "retry_authorized": False,
                    }
            except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                pass  # Actual live/unknown/missing C remains owned; never retry.
        return {
            "kind": "held",
            "reason": "coding_worker_already_owned",
            "execution_authorized": False,
            "retry_authorized": False,
        }
    stage = "original_terminal"
    try:
        with continuation._coding_scope(campaign, document, manifest, manifest_path) as scope:
            reference = row["reference"]
            # No claim/admission/publication until all original workers and their
            # actual signed known C/history/returned issuance are authenticated.
            coding_original_terminal_for_tick(scope, reference)
            if any(
                w.get("state") != "terminal"
                or w.get("outcome") not in {"ok", "unavailable", "scoped_continuation"}
                for w in document.get("workers", [])
            ):
                raise mechanisms.MechanismError("original workers not lawfully terminal")
            stage = "admission"
            context = publish_coding_admission(scope, reference)
            selector = context["coding_continuation"]
            _advance_coding_registry(scope, reference, "admitted", worker=selector)
            stage = "claim"
            if claims.holder(selector["claim_target"]) is not None or not claims.claim(
                selector["claim_target"], campaign_source.AGENT
            ):
                raise mechanisms.MechanismError("coding claim already owned")
            stage = "launch_intent"
            prepare_coding_launch(scope, reference)
            _prepared = True
            stage = "launch_observation"
            return launch_coding_worker(scope, reference)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        # Preserve every durable admission/intent/claim/unknown. Never release or
        # recycle partial publication based on an exception/expired clock.
        return {
            "kind": "held",
            "reason": "coding_" + stage + "_held",
            "execution_authorized": False,
            "retry_authorized": False,
        }


def publish_coding_admission(scope, reference):
    """One protected fresh coding admission under the existing native tick lifetime."""
    import closer_coding_continuation as continuation
    import closer_correction_controller as ctl
    import closer_frontier_policy as frontier
    import closer_operational_capacity as capacity
    import closer_source_installation as installed
    from closer_campaign import ROUND_UNIT

    if type(scope) is not continuation._CodingScope:
        from closer_correction_source_record import SourceRecordUnavailable

        raise SourceRecordUnavailable("actual coding tick source scope required")
    scope.check()
    ref = continuation.validate_reference(reference)
    record = continuation.read_tick_selected(scope, ref)
    document, manifest = scope.document, scope.manifest
    registry = continuation.validate_registry(document[continuation.REGISTRY_FIELD])
    selected = registry[ref["stable_key"]]
    if (
        selected["state"] != "awaiting_original_terminal"
        or any(
            row["state"] not in {"awaiting_original_terminal", "terminal"}
            for key, row in registry.items()
            if key != ref["stable_key"]
        )
        or document.get("phase") != "work"
        or document.get("hold")
        or document.get("targets") != {ROUND_UNIT: "reconcile"}
        or any(
            w.get("state") != "terminal"
            or w.get("outcome") not in {"ok", "unavailable", "scoped_continuation"}
            for w in document.get("workers", [])
        )
    ):
        raise mechanisms.MechanismError("fresh coding admission requires closed original workers")
    history = coding_original_terminal_for_tick(scope, ref)
    value, pinned = installed._qualified_policy(scope.binding)
    if pinned != manifest or value["manifest_sha256"] != scope.manifest_sha256:
        raise mechanisms.MechanismError("coding publisher source knowledge changed")
    config = ctl.ControllerConfig(**value["config"])
    purpose = _coding_purpose_projection(config, record)
    import closer_coding_session_runtime as coding_runtime
    import closer_coding_session_v2 as coding_session
    import execution_profiles

    consumer = config.consumers[record["consumer_id"]]
    coding_policy = coding_session.validate_policy(consumer[coding_session.POLICY_FIELD])
    coding_session.minimum_budget(
        record["original_snapshot"]["budget"],
        coding_session.validate_budget(consumer[coding_runtime.CURRENT_FIELD]),
        coding_policy["budget"],
    )
    profile = execution_profiles.get_profile(coding_session.PROFILE)
    if (
        consumer.get("profile_id") != coding_session.PROFILE
        or consumer.get("agent") != "codex"
        or consumer.get("owner_login") != record["owner_login"]
        or consumer.get("profile_sha256") != ctl.source_digest(profile)
        or profile.get("lifecycle_status") != "active"
        or "offload" not in profile.get("transport_support", [])
    ):
        raise mechanisms.MechanismError("source coding profile/current owner policy differs")
    policy = capacity_contract(manifest, control_plane_only=True)
    if policy is None or policy["schema"] != "native-scoped-capacity/v2":
        raise mechanisms.MechanismError("explicit bounded native coding policy unavailable")
    root = contract(manifest)[3]
    nonce = _CURRENT_ENTRY.get(str(root))
    invocation = _CURRENT_ADMISSION.get(str(root))
    if (
        not entry_active(manifest)
        or nonce != scope.nonce
        or not invocation
        or invocation.get("nonce") != nonce
        or invocation.get("context") is not None
    ):
        raise mechanisms.MechanismError("fresh source native coding invocation unavailable")
    cfg = document["worker_config"]
    workspace = cfg.get("workspaces")
    if type(workspace) is not dict or set(workspace) != {"path", "sha256"}:
        raise mechanisms.MechanismError("coding requires original authorized workspace pin")
    workspace_path = Path(workspace["path"])
    workspace_raw = _bounded_bytes(workspace_path, 1024 * 1024)
    if hashlib.sha256(workspace_raw).hexdigest() != workspace["sha256"]:
        raise mechanisms.MechanismError("coding authorized workspace changed")
    from closer_dispatcher import load_workspace_config

    load_workspace_config(
        workspace_path,
        workspace["sha256"],
        require_pin=True,
        targets=[purpose["target"]],
        validate_execution=False,
    )
    worker_id = secrets.token_hex(6)
    revision = ctl.digest({"reference": ref, "worker_id": worker_id, "nonce": nonce})
    binding_value = _coding_dispatch_binding(config, value["policy_projection"], purpose, ref)
    descriptor = frontier.make_frontier(
        [purpose["target"]],
        owner=document["owner"],
        frontier_id="coding-" + worker_id,
        bindings={
            purpose["target"]: {
                "revision": revision,
                "action_id": frontier.action_identity(
                    purpose["target"], revision, "correction_coding", binding_value
                ),
            }
        },
    )
    selector = {
        "schema": _CODING_SELECTOR_SCHEMA,
        "worker_id": worker_id,
        "target": ROUND_UNIT,
        "claim_target": document["campaign_id"] + ":coding:" + ref["stable_key"],
        "nonce": nonce,
        "reference": ref,
        "source_target": history["enrollment_boundary"]["target"],
        "coding_target": purpose["target"],
        "queue_state": str(Path(config.state_path + ".coding") / (ref["stable_key"] + ".json")),
        "finite_frontier": descriptor,
        "request_timeout_seconds": policy["request_timeout_seconds"],
        "purpose": purpose,
    }
    _validate_coding_selector(selector, document, config, record, value["policy_projection"])
    if Path(selector["queue_state"]).exists() or Path(selector["queue_state"]).is_symlink():
        raise mechanisms.MechanismError("coding queue already exists; no repeat or renewal")
    directory = root / nonce
    report_path = directory / "scoped-capacity.json"
    # Control evidence grants NO closer classification/adjudication/model role.
    # Existing coding pools and installed consumer/profile/account/budget/final
    # before-Popen guard remain mandatory at the actual fresh worker callback.
    report = capacity.publish(
        {
            "schema": capacity.SCHEMA,
            "owner": policy["owner"],
            "profile_ids": [],
            "bounded_trial_agents": [],
            "control_plane_only": True,
            "original_deadline": time.time() + policy["freshness_seconds"],
            "runtime_manifest": scope.manifest_path,
            "runtime_manifest_sha256": scope.manifest_sha256,
        },
        report_path,
        expected_sha256=None,
    )
    if not capacity.validate_report(report, now=time.time()):
        raise mechanisms.MechanismError("fresh coding control-plane evidence unavailable")
    assignment = directory / "assignment.json"
    raw = json.dumps(
        {
            "schema": "closer-coding-assignment/v1",
            "reference": ref,
            "source_target": selector["source_target"],
            "parent_target": ROUND_UNIT,
            "targets": [{"target": purpose["target"]}],
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    scope.check()
    with contextlib.closing(ctl._Custody(config)) as custody:
        intent_name = "coding-admission-intent-" + ref["stable_key"] + ".json"
        try:
            custody.read(intent_name)
        except FileNotFoundError:
            pass
        else:
            raise mechanisms.MechanismError("coding admission intent already owned; never renew")
        custody.create(
            intent_name,
            {
                "schema": "closer-coding-admission-intent/v1",
                "reference": ref,
                "worker_json": json.dumps(
                    selector, sort_keys=True, separators=(",", ":"), allow_nan=False
                ),
                "manifest_sha256": scope.manifest_sha256,
            },
        )
        _immutable(assignment, raw)
        _immutable(directory / "workspaces.json", workspace_raw)
        context = {
            "schema": "native-invocation-admission/v2",
            "nonce": nonce,
            "started_at": invocation["started_at"],
            "campaign": _campaign_identity(Path(config.state_path), document),
            "assignment": {
                "path": str(assignment),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "source_path": str(assignment),
            },
            "report": {"path": str(report_path), "sha256": _hash(report_path)},
            "runtime_manifest": scope.manifest_path,
            "runtime_manifest_sha256": scope.manifest_sha256,
            "workspace_config": {
                "path": str(directory / "workspaces.json"),
                "sha256": workspace["sha256"],
                "source_path": str(workspace_path),
            },
            "finite_frontier": descriptor,
            "request_timeout_seconds": policy["request_timeout_seconds"],
            "coding_continuation": selector,
        }
        admission_path = directory / "admission.json"
        if admission_path.exists() or admission_path.is_symlink():
            raise mechanisms.MechanismError("native coding admission already exists")
        signed_record = {"binding": binding(manifest), "context": context}
        _immutable(
            admission_path,
            json.dumps(
                {
                    "record": signed_record,
                    "signature": mechanisms._signature(
                        signed_record, _existing_lifecycle_key(root)
                    ),
                },
                sort_keys=True,
            ).encode(),
        )
        invocation["context"] = context
    scope.check()
    return context


def coding_original_terminal_for_tick(scope, reference):
    """Original known-scoped prerequisite; never a worker execution installation."""
    import closer_coding_continuation as continuation
    import closer_correction_controller as ctl

    if type(scope) is not continuation._CodingScope:
        from closer_correction_source_record import SourceRecordUnavailable

        raise SourceRecordUnavailable("actual coding tick source scope required")
    scope.check()
    continuation.read_tick_selected(scope, reference)
    config = ctl.ControllerConfig(
        **scope.manifest["policy"]["correction_controller_installation"]["config"]
    )
    history = _coding_original_history_knowledge(scope.manifest, scope.document, config, reference)
    scope.check()
    return history


def coding_original_history(installation, reference):
    """Actual accepted coding WORKER only; current and original identities stay distinct."""
    import closer_coding_worker as worker
    import closer_correction_controller as ctl
    from closer_correction_source_record import SourceRecordUnavailable

    frame = inspect.currentframe().f_back
    try:
        root = frame.f_locals.get("root")
        if (
            frame.f_code is not worker._original_history.__code__
            or frame.f_globals is not worker.__dict__
            or type(root) is not worker._Root
            or root.installation is not installation
        ):
            raise SourceRecordUnavailable("actual accepted worker historical reader required")
        root.check()
        if json.loads(root.accepted_utf8)["continuation_reference"] != reference:
            raise SourceRecordUnavailable("accepted worker historical reference differs")
        doc = ctl.IndexController._physical_state(
            installation.config["state_path"], "coding campaign redirected"
        )
        history = _coding_original_history_knowledge(
            root.manifest, doc, ctl.ControllerConfig(**installation.config), reference
        )
        root.check()
        return history
    finally:
        del frame


def _retained_enrollment_cleanup(manifest, campaign_doc, marker, marker_ref):
    """Historical enrollment correlation only; no synthetic instruction plan."""
    import closer_campaign as campaign_source
    import closer_correction_controller as ctl

    ref = marker["native_b_start"]
    root = contract(manifest)[3]
    path = Path(ref["path"])
    if (
        path.resolve() != path
        or path.parent.parent != root
        or not re.fullmatch(r"[a-f0-9]{32}", path.parent.name)
        or not re.fullmatch(r"worker-b-start-[a-f0-9]{12}\.json", path.name)
    ):
        raise mechanisms.MechanismError("retained cleanup selector escaped original native custody")
    key = _existing_lifecycle_key(root)
    start, exact_ref = _read_lifecycle_record(path.parent, path.name, key)
    if ref != exact_ref or start.get("admission", {}).get("nonce") != path.parent.name:
        raise mechanisms.MechanismError("retained cleanup B-start bytes differ")
    context, fields = start["admission"], start["worker"]
    worker_id = start["worker_id"]
    workers = [row for row in campaign_doc.get("workers", []) if row.get("worker_id") == worker_id]
    if len(workers) != 1 or workers[0].get("state") != "terminal":
        raise mechanisms.MechanismError("retained source worker is not independently terminal")
    original_cfg = result_admission_config(
        campaign_doc["worker_config"],
        context,
        Path(context["campaign"]["state"]),
        campaign_doc,
        workers[0],
    )
    directory, original_key, _context, expected_fields, expected_id, handoff_sha = (
        _lifecycle_authority(manifest, original_cfg)
    )
    if (
        directory != path.parent
        or original_key != key
        or expected_fields != fields
        or expected_id != worker_id
        or start.get("handoff_sha256") != handoff_sha
        or start.get("binding") != binding(manifest)
        or start.get("schema") != "closer-native-correction-b-start/v1"
        or start.get("worker_config_sha256") != mechanisms.digest(original_cfg)
        or start.get("producer_sha256") != manifest["modules"]["closer_runtime_binding.py"]
        or start.get("process", {}).get("pid")
        != marker["instruction_effect"]["owner"].get("process_id")
    ):
        raise mechanisms.MechanismError("retained B/source/worker configuration differs")
    terminal, terminal_ref = _read_lifecycle_record(
        directory, "terminal-cleanup-" + worker_id + ".json", key
    )
    if (
        terminal.get("schema") != "closer-native-correction-terminal-cleanup/v1"
        or terminal.get("binding") != binding(manifest)
        or terminal.get("admission") != context
        or terminal.get("worker") != fields
        or terminal.get("worker_id") != worker_id
        or terminal.get("handoff_sha256") != handoff_sha
        or terminal.get("native_b_start") != ref
        or terminal.get("process") != start["process"]
        or terminal.get("argv_sha256") != start.get("argv_sha256")
        or terminal.get("producer_sha256") != manifest["modules"]["closer_runtime_binding.py"]
        or terminal.get("coordinator_group_terminal") is not True
        or any(
            terminal.get(name) is not False
            for name in (
                "provider_completion_asserted",
                "domain_completed",
                "acceptance_authorized",
                "execution_authorized",
                "source_closure_authorized",
                "remote_execution_attested",
            )
        )
    ):
        raise mechanisms.MechanismError("retained source terminal-only cleanup differs")
    campaign_source._completion_cleanup(original_cfg, terminal, "unknown")
    result = ctl.IndexController._physical_state(
        fields["result"], "original native worker result redirected"
    )
    detail = json.loads(result["detail"])
    if (
        result.get("worker_id") != worker_id
        or result.get("target") != fields["target"]
        or result.get("mechanism") != mechanisms.NATIVE
        or result.get("native_admission") != context
        or result.get("outcome") not in {"unknown", "ok", "scoped_continuation"}
        or workers[0].get("outcome") != result.get("outcome")
        or detail.get("native_terminal_cleanup") != terminal_ref
        or detail.get("native_execution") != terminal.get("native_execution")
    ):
        raise mechanisms.MechanismError("original native worker result/cleanup join differs")
    links = terminal.get("operations")
    if type(links) is not list or len(links) > 128:
        raise mechanisms.MechanismError("retained cleanup operation inventory unavailable")
    matched = [
        row
        for row in links
        if row.get("schema") == "closer-native-enrollment-operation-link/v1"
        and row.get("enrollment_terminal_ref") == marker_ref
    ]
    if len(matched) != 1:
        raise mechanisms.MechanismError("retained cleanup lacks exact original enrollment link")
    link = matched[0]
    operation = json.loads(marker["operation_json"])
    if (
        workers[0].get("outcome") != "scoped_continuation"
        or result.get("outcome") != "scoped_continuation"
        or link.get("operation_id") != operation.get("id")
        or link.get("operation_sha256") != marker["operation_sha256"]
        or link.get("native_b_start") != ref
        or link.get("instruction_intent_sha256") != marker["instruction_effect"]["intent_sha256"]
        or link.get("retained_status") != "completed"
        or link.get("provider_completion_asserted") is not False
    ):
        raise mechanisms.MechanismError("retained enrollment original known terminal differs")
    proof = detail.get("proof")
    if (
        type(proof) is not dict
        or proof.get("schema") != "native-scoped-continuation/v1"
        or proof.get("domain_outcome") != "known"
        or proof.get("accepted") is not False
        or proof.get("global_stop_valid") is not False
        or proof.get("finite_frontier") != marker["finite_frontier"]
    ):
        raise mechanisms.MechanismError("original enrollment lacks exact known scoped outcome")
    import closer_resources
    from closer_dispatcher import targets_from_assignment

    targets = targets_from_assignment(Path(original_cfg["assignment"]))
    verdict = closer_resources.validate_stop(
        Path(str(original_cfg["dispatch_state"]) + ".pipeline.json"), proof["round_id"], targets
    )
    if (
        verdict.get("admitted_domain_valid") is not True
        or verdict.get("admitted_domain_reasons")
        or verdict.get("global_stop_valid") is not False
        or verdict.get("run_ids") != proof.get("run_ids")
        or verdict.get("receipt_sha256") != proof.get("receipt_sha256")
    ):
        raise mechanisms.MechanismError("original known enrollment scoped receipt changed")
    return {
        "start": start,
        "terminal": terminal,
        "terminal_ref": terminal_ref,
        "original_config": original_cfg,
        "operation_link": link,
    }


def _process_public(value):
    return {key: value[key] for key in ("pid", "started", "argv_sha256", "source")}


def _request(path, manifest):
    root = contract(manifest)[3]
    path = Path(path)
    if (
        path.parent.parent != root
        or not re.fullmatch(r"[0-9a-f]{32}", path.parent.name)
        or path.name != "request.json"
        or path.parent.is_symlink()
    ):
        raise mechanisms.MechanismError("native request path is outside private observation root")
    request = _read(path)
    action, hard = request.get("action_deadline"), request.get("hard_deadline")
    if (
        request.get("schema") != REQUEST
        or request.get("nonce") != path.parent.name
        or request.get("binding") != binding(manifest)
        or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
            for v in (action, hard)
        )
        or hard - action != 5
        or not 0 < hard - time.monotonic() <= 120
    ):
        raise mechanisms.MechanismError("native request binding/deadline differs")
    return request


def _native_child(manifest, request_path, role):
    request = _request(request_path, manifest)
    _value, _wrapper, _python, root = contract(manifest)
    if request.get("schema") != REQUEST or request.get("binding") != binding(manifest):
        return 78
    directory = Path(request_path).parent
    if directory.parent != root or directory.is_symlink():
        return 78
    deadline = request["action_deadline"]
    if (
        not isinstance(deadline, (int, float))
        or not math.isfinite(deadline)
        or time.monotonic() >= deadline
    ):
        return 78
    if role == "first":
        _write(directory / "child-ready.json", {"nonce": request["nonce"], "pid": os.getpid()})
        handoff = _wait(directory / "child-handoff.json", deadline)
        if handoff.get("nonce") != request["nonce"]:
            return 78
        _write(
            directory / "child-journal.json",
            {"nonce": request["nonce"], "binding": request["binding"], "pid": os.getpid()},
        )
        while time.monotonic() < deadline:
            time.sleep(min(0.02, max(0, deadline - time.monotonic())))
        return 78
    _write(directory / "restart-ready.json", {"nonce": request["nonce"], "pid": os.getpid()})
    if _wait(directory / "restart-ack.json", deadline).get("nonce") != request["nonce"]:
        return 78
    journal = _read(directory / "child-journal.json")
    if journal.get("nonce") != request["nonce"] or journal.get("binding") != request["binding"]:
        return 78
    _write(
        directory / "child-result.json",
        {**journal, "restarted_pid": os.getpid(), "no_effect": True},
    )
    return 0


def _qualification_lifecycle(manifest, manifest_path, request, directory):
    """Only known no-effect children may be terminated; never a provider/queue child."""
    from closer_mechanisms import _identity

    python = contract(manifest)[2]
    base = [
        str(python),
        manifest["launcher"]["path"],
        "native-noeffect-child",
        "--manifest",
        str(manifest_path),
        "--request",
        str(directory / "request.json"),
    ]
    deadline = request["action_deadline"]
    children = []
    denial = None
    try:
        first_argv = [*base, "--role", "first"]
        first = subprocess.Popen(
            first_argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        children.append(first)
        _wait(directory / "child-ready.json", deadline)
        identity = _identity(first.pid, first_argv, deadline=deadline)
        _write(directory / "child-handoff.json", {"nonce": request["nonce"]})
        journal = _wait(directory / "child-journal.json", deadline)
        if journal.get("pid") != first.pid or journal.get("binding") != request["binding"]:
            raise mechanisms.MechanismError("no-effect handoff journal identity differs")
        # Exact birth and argv immediately before terminating our known child.
        if _identity(first.pid, first_argv, deadline=deadline) != identity:
            raise mechanisms.MechanismError("no-effect child changed before controlled crash")
        first.kill()
        first.communicate(timeout=max(0.001, deadline - time.monotonic()))
        if first.returncode != -signal.SIGKILL:
            raise mechanisms.MechanismError("controlled no-effect crash was not observed")
        _write(
            directory / "crash.json",
            {
                "first": _process_public(identity),
                "exit": first.returncode,
                "nonce": request["nonce"],
                "no_effect": True,
            },
        )
        restart_argv = [*base, "--role", "restart"]
        restart = subprocess.Popen(
            restart_argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        children.append(restart)
        _wait(directory / "restart-ready.json", deadline)
        restart_identity = _identity(restart.pid, restart_argv, deadline=deadline)
        _write(directory / "restart-ack.json", {"nonce": request["nonce"]})
        stdout, stderr = restart.communicate(timeout=max(0.001, deadline - time.monotonic()))
        if restart.returncode != 0:
            raise mechanisms.MechanismError(f"no-effect restart refused ({restart.returncode})")
        result = _read(directory / "child-result.json")
        if (
            result.get("pid") != first.pid
            or result.get("restarted_pid") != restart.pid
            or result.get("binding") != request["binding"]
        ):
            raise mechanisms.MechanismError("no-effect restart result differs")
        return {
            "first": _process_public(identity),
            "restart": _process_public(restart_identity),
            "restart_pid": restart.pid,
            "crash_exit": first.returncode,
            "result_sha256": _hash(directory / "child-result.json"),
        }
    except PermissionError as exc:
        denial = {"type": type(exc).__name__, "errno": exc.errno, "reason": str(exc)}
        _write(directory / "denial.json", denial)
        raise
    finally:
        # No alternate signal after a denial. Children self-expire at the same
        # original action deadline; retain UNKNOWN if their terminality is absent.
        for child in children:
            if child.poll() is None:
                try:
                    child.communicate(
                        timeout=max(0.001, request["hard_deadline"] - time.monotonic())
                    )
                except subprocess.TimeoutExpired:
                    if denial is None:
                        _write(
                            directory / "cleanup-pending.json", {"pid": child.pid, "unknown": True}
                        )


def _own_identity(pid, argv, deadline):
    if pid not in {os.getpid(), os.getppid()}:
        raise mechanisms.MechanismError("native identity is outside own immediate lineage")
    before = mechanisms._metadata(pid, deadline)
    proof = mechanisms._argv_proof(before, deadline, ancestor=True)
    if proof != {"argc": len(argv), "argv_sha256": mechanisms._argv_digest(argv)}:
        raise mechanisms.MechanismError("native wrapper/entry OS argv differs from exact pin")
    if mechanisms._metadata(pid, deadline) != before:
        raise mechanisms.MechanismError("native wrapper/entry birth changed")
    return {
        "pid": pid,
        "started": before["started"],
        "argv_sha256": proof["argv_sha256"],
        "source": "os-byte-argv",
    }


def entry_active(manifest, *, _scope=None):
    """Only the observed, lock-held entry in this process may schedule work."""
    _value, _wrapper, _python, root = contract(manifest, _scope=_scope)
    active = _read(root / "native-active.json")
    return (
        active.get("binding") == binding(manifest, _scope=_scope)
        and active.get("status") == "submitted"
        and _CURRENT_ENTRY.get(str(root)) == active.get("nonce")
    )


def authorize_worker(manifest, argv):
    if "private_test" in (manifest.get("mechanism_contract") or {}):
        raise mechanisms.MechanismError("private metadata test cannot authorize a worker")
    from closer_correction_maintenance import assert_offload_allowed

    assert_offload_allowed()
    if not entry_active(manifest):
        raise mechanisms.MechanismError("native worker handoff requires observed entry")
    if not argv or argv[0] != str(contract(manifest)[2]):
        raise mechanisms.MechanismError("native worker requires pinned physical interpreter")
    fields = {}
    for flag in (
        "state",
        "target",
        "owner",
        "generation",
        "runtime-manifest",
        "mechanism",
        "result",
    ):
        key = "--" + flag
        if argv.count(key) != 1:
            raise mechanisms.MechanismError("native worker handoff argument differs")
        fields[flag] = argv[argv.index(key) + 1]
    if fields["mechanism"] != mechanisms.NATIVE:
        raise mechanisms.MechanismError("native worker handoff mechanism differs")
    result = Path(fields["result"])
    if not re.fullmatch(r"[0-9a-f]{12}", result.stem):
        raise mechanisms.MechanismError("native worker identity is invalid")
    _value, _wrapper, _python, root = contract(manifest)
    nonce = _CURRENT_ENTRY[str(root)]
    handoff_path = root / nonce / ("worker-" + result.stem + ".json")
    if handoff_path.exists() or handoff_path.is_symlink():
        raise mechanisms.MechanismError("native worker handoff already exists")
    _signed(
        handoff_path,
        {
            "schema": "native-worker-handoff/v1",
            "binding": binding(manifest),
            "nonce": nonce,
            "fields": fields,
            "argv_sha256": mechanisms.digest(argv),
            **(
                {"admission": _CURRENT_ADMISSION[str(root)]["context"]}
                if capacity_contract(manifest, control_plane_only=True) is not None
                else {}
            ),
        },
        root,
    )


def coding_worker_registration(
    document, *, state, result, target, owner, generation, manifest_path
):
    """Private registry membership joins signed launch and this actual OS worker."""
    import closer_campaign as campaign_source
    import closer_coding_continuation as continuation

    frame = inspect.currentframe().f_back
    if (
        frame.f_code is not campaign_source.run_worker.__code__
        or frame.f_globals is not campaign_source.__dict__
        or frame.f_locals.get("doc") is not document
        or frame.f_locals.get("state") != state
        or frame.f_locals.get("result") != result
        or frame.f_locals.get("runtime_manifest") != manifest_path
    ):
        raise mechanisms.MechanismError("actual registered coding worker entry required")
    manifest = campaign_source.load_manifest(manifest_path)
    registry = continuation.validate_registry(document.get(continuation.REGISTRY_FIELD, {}))
    matches = [
        row
        for row in registry.values()
        if (row.get("worker") or {}).get("worker_id") == Path(result).stem
    ]
    if len(matches) != 1:
        raise mechanisms.MechanismError("unique source registered coding worker required")
    (row,) = matches
    selector = row["worker"]
    if (
        row["state"] not in {"submitted", "active"}
        or target != selector["target"]
        or document.get("owner") != owner
        or document.get("generation", 1) != generation
        or document.get("phase") != "work"
        or document.get("hold")
        or any(
            w.get("state") != "terminal"
            or w.get("outcome") not in {"ok", "unavailable", "scoped_continuation"}
            for w in document.get("workers", [])
        )
    ):
        raise mechanisms.MechanismError("coding worker original/registration fence remains held")
    context = accept_worker(
        manifest,
        state=state,
        result=result,
        target=target,
        owner=owner,
        generation=generation,
        manifest_path=manifest_path,
        campaign_id=document["campaign_id"],
    )
    if context.get("coding_continuation") != selector:
        raise mechanisms.MechanismError("accepted coding worker selector differs")
    launch = _coding_stage_record(
        manifest, selector, "coding-launch-intent-" + selector["worker_id"] + ".json"
    )
    observed = _coding_stage_record(
        manifest, selector, "coding-launch-observation-" + selector["worker_id"] + ".json"
    )
    argv = _coding_launch_argv(launch)
    identity = _own_identity(os.getpid(), argv, None)
    if (
        observed.get("schema") != "closer-native-coding-launch-observation/v1"
        or observed.get("worker") != selector
        or observed.get("reference") != row["reference"]
        or observed.get("intent_sha256") != mechanisms.digest(launch)
        or {k: observed.get("process", {}).get(k) for k in identity} != identity
    ):
        raise mechanisms.MechanismError("coding worker actual signed process birth differs")
    return {**selector, "state": row["state"]}


def coding_dispatch_selected(cfg):
    """Only the existing run_worker may select its accepted private dispatcher."""
    import closer_campaign as campaign_source

    dispatch = inspect.currentframe().f_back
    worker = dispatch.f_back
    if (
        dispatch.f_code is not campaign_source.dispatch_command.__code__
        or dispatch.f_globals is not campaign_source.__dict__
        or dispatch.f_locals.get("cfg") is not cfg
        or worker.f_code is not campaign_source.run_worker.__code__
        or worker.f_globals is not campaign_source.__dict__
        or worker.f_locals.get("cfg") is not cfg
    ):
        raise mechanisms.MechanismError("actual accepted coding worker dispatch required")
    manifest = worker.f_locals.get("pinned_worker")
    context = worker.f_locals.get("worker_admission")
    validate_admission(manifest, context, for_effects=True)
    selector = context.get("coding_continuation")
    if (
        type(selector) is not dict
        or cfg.get("coding_continuation") != selector["reference"]
        or cfg.get("native_admission") != context
        or cfg.get("dispatch_state") != selector["queue_state"]
        or cfg.get("finite_frontier") != selector["finite_frontier"]
        or cfg.get("request_timeout_seconds") != selector["request_timeout_seconds"]
    ):
        raise mechanisms.MechanismError("coding dispatcher differs from fresh signed admission")
    return True


def accept_worker(
    manifest, *, state, target, owner, generation, result, manifest_path, campaign_id=None
):
    """Reconstitute only a handoff issued before this exact durable worker launch."""
    _value, _wrapper, _python, root = contract(manifest)
    worker_id = Path(result).stem
    if not re.fullmatch(r"[0-9a-f]{12}", worker_id):
        raise mechanisms.MechanismError("native worker identity is invalid")
    active = _read(root / "native-active.json")
    nonce = active.get("nonce", "")
    if not re.fullmatch(r"[0-9a-f]{32}", nonce):
        raise mechanisms.MechanismError("native invocation identity missing")
    signed = _read(root / nonce / ("worker-" + worker_id + ".json"))
    record = signed["record"]
    fields = {
        "state": state,
        "target": target,
        "owner": owner,
        "generation": str(generation),
        "result": result,
        "runtime-manifest": manifest_path,
        "mechanism": mechanisms.NATIVE,
    }
    if (
        not hmac.compare_digest(
            signed["signature"], mechanisms._signature(record, mechanisms._key(root))
        )
        or record.get("schema") != "native-worker-handoff/v1"
        or record.get("binding") != binding(manifest)
        or record.get("nonce") != nonce
        or record.get("fields") != fields
        or active.get("binding") != binding(manifest)
    ):
        raise mechanisms.MechanismError("native worker handoff differs from exact invocation")
    # This process already passed campaign owner/generation/claim/worker fences.
    # Its signed pre-launch handoff permits qualification during the entry's
    # short submitted-to-terminal transition; arbitrary JSON never does.
    context = record.get("admission")
    if capacity_contract(manifest, control_plane_only=True) is not None:
        validate_admission(manifest, context, for_effects=True)
        if context["campaign"] != {
            "state": str(Path(state).resolve()),
            "campaign_id": campaign_id,
            "owner": owner,
            "generation": generation,
        }:
            raise mechanisms.MechanismError("native worker admission belongs to another campaign")
    _CURRENT_ENTRY[str(root)] = nonce
    return context


def native_entry(manifest, manifest_path, campaign_root):
    # The qualification supervisor already holds this guard in its parent.
    # Every normal invocation holds the same guard through durable terminality,
    # so qualification cannot replace another entry's submitted intent.
    if os.environ.get("CLOSER_NATIVE_QUALIFICATION_REQUEST"):
        return _native_entry(manifest, manifest_path, campaign_root)
    root = contract(manifest)[3]
    with (root / "native-supervisor.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise mechanisms.MechanismError(
                "native supervisor or wrapper entry already active"
            ) from exc
        return _native_entry(manifest, manifest_path, campaign_root)


def _native_entry(manifest, manifest_path, campaign_root):
    """Production wrapper entry; policy and observed exact wrapper precede any tick."""
    value, wrapper, _python, root = _check_wrapper_entry(manifest, manifest_path)
    if str(campaign_root) != value["campaign_root"]:
        raise mechanisms.MechanismError("native entry campaign root differs from pin")
    request_name = os.environ.get("CLOSER_NATIVE_QUALIFICATION_REQUEST")
    request = _request(request_name, manifest) if request_name else None
    if not request:
        problems = qualification_problems(manifest)
        if problems:
            raise mechanisms.MechanismError("; ".join(problems))
    deadline = request["action_deadline"] if request else time.monotonic() + 30
    wrapper_identity = _own_identity(
        os.getppid(), ["/bin/bash", str(wrapper), "closer"], deadline=deadline
    )
    entry_identity = _own_identity(
        os.getpid(), _entry_argv(manifest, manifest_path), deadline=deadline
    )
    nonce = request["nonce"] if request else secrets.token_hex(16)
    directory = root / nonce
    if request:
        if (
            request.get("schema") != REQUEST
            or request.get("binding") != binding(manifest)
            or Path(request_name) != directory / "request.json"
            or _read(root / "native-active.json").get("nonce") != nonce
        ):
            raise mechanisms.MechanismError("native qualification request identity differs")
    else:
        directory.mkdir(mode=0o700)
    with (root / "native-entry.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise mechanisms.MechanismError("native wrapper entry already active") from exc
        if (
            not request
            and (root / "native-active.json").exists()
            and _read(root / "native-active.json").get("status") != "terminal"
        ):
            raise mechanisms.MechanismError("prior native wrapper submission remains unknown")
        _write(
            root / "native-active.json",
            {"nonce": nonce, "status": "submitted", "binding": binding(manifest)},
        )
        observation = {
            "schema": OBSERVATION,
            "binding": binding(manifest),
            "nonce": nonce,
            "actor": value["actor"],
            "source": "owned-native-wrapper-process",
            "desktop_qualified": False,
            "scheduler_origin": "unobserved",
            "wrapper": _process_public(wrapper_identity),
            "entry": _process_public(entry_identity),
        }
        _write(directory / "startup.json", observation)
        try:
            if request:
                if _wait(directory / "observer-ack.json", deadline).get("nonce") != nonce:
                    raise mechanisms.MechanismError("native observer acknowledgement differs")
                lifecycle = _qualification_lifecycle(manifest, manifest_path, request, directory)
                observation.update(
                    kind="native-wrapper-qualification",
                    passed=True,
                    lifecycle=lifecycle,
                    phases={
                        phase: {"file": name, "sha256": _hash(directory / name)}
                        for phase, name in {
                            "startup": "startup.json",
                            "handoff": "child-journal.json",
                            "crash_restart": "crash.json",
                            "result_recording": "child-result.json",
                        }.items()
                    },
                )
            else:
                from closer_campaign import tick

                # In-process capability permits this lock-held entry to recheck
                # qualification while its own submission is in progress. JSON
                # alone never issues that capability to another worker process.
                _CURRENT_ENTRY[str(root)] = nonce
                capacity_policy = capacity_contract(manifest, control_plane_only=True)
                if capacity_policy is not None:
                    started = time.time()
                    _CURRENT_ADMISSION[str(root)] = {
                        "nonce": nonce,
                        "started_at": started,
                        **(
                            {"execution_policy": "finite-frontier/v2"}
                            if capacity_policy["schema"] == "native-scoped-capacity/v2"
                            else {
                                "original_deadline": started + capacity_policy["admission_seconds"]
                            }
                        ),
                    }
                try:
                    result = tick(campaign_root, str(manifest_path), mechanism=mechanisms.NATIVE)
                finally:
                    _CURRENT_ENTRY.pop(str(root), None)
                    _CURRENT_ADMISSION.pop(str(root), None)
                observation.update(
                    kind="native-wrapper-invocation", passed=True, tick_result=result
                )
            _signed(directory / "terminal.json", observation, root)
            _write(
                root / "native-active.json",
                {"nonce": nonce, "status": "terminal", "binding": binding(manifest)},
            )
            return 0
        except BaseException as exc:
            observation.update(
                passed=False,
                unknown=True,
                error={
                    "type": type(exc).__name__,
                    "errno": getattr(exc, "errno", None),
                    "reason": str(exc),
                },
            )
            _signed(directory / "terminal.json", observation, root)
            raise


def supervise_native(manifest, manifest_path, *, budget_seconds):
    """Actually invoke the opted-in reviewed wrapper, with no provider/queue path."""
    manifest_path = Path(manifest_path).resolve()
    value, wrapper, _python, root = _check_wrapper_entry(manifest, manifest_path)
    if (
        not isinstance(budget_seconds, (int, float))
        or isinstance(budget_seconds, bool)
        or not 10 <= budget_seconds <= 120
    ):
        raise mechanisms.MechanismError("native qualification budget must be 10..120 seconds")
    with (root / "native-supervisor.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise mechanisms.MechanismError("native qualification already active") from exc
        active = root / "native-active.json"
        if active.exists() and _read(active).get("status") != "terminal":
            raise mechanisms.MechanismError("native submission remains unknown; no relaunch")
        mechanisms._key(root, create=True)
        nonce = secrets.token_hex(16)
        directory = root / nonce
        directory.mkdir(mode=0o700)
        hard = time.monotonic() + budget_seconds
        request = {
            "schema": REQUEST,
            "nonce": nonce,
            "binding": binding(manifest),
            "action_deadline": hard - 5,
            "hard_deadline": hard,
        }
        _write(directory / "request.json", request)
        _write(active, {"nonce": nonce, "binding": request["binding"], "status": "submitted"})
        env = dict(
            os.environ,
            CLOSER_NATIVE_QUALIFICATION_REQUEST=str(directory / "request.json"),
            PYTHONDONTWRITEBYTECODE="1",
        )
        from closer_launcher import CREDENTIAL_VARS

        for key in CREDENTIAL_VARS:
            env.pop(key, None)
        proc = subprocess.Popen(
            ["/bin/bash", str(wrapper), "closer"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            while not (directory / "startup.json").is_file():
                if proc.poll() is not None:
                    stdout, stderr = proc.communicate(timeout=max(0.001, hard - time.monotonic()))
                    _write(
                        directory / "wrapper-refusal.json",
                        {"exit": proc.returncode, "stdout": stdout, "stderr": stderr},
                    )
                    raise mechanisms.MechanismError(
                        "native wrapper refused before observation: "
                        + str(proc.returncode)
                        + ": "
                        + (stdout + stderr)[-2000:]
                    )
                if time.monotonic() >= hard - 5:
                    raise mechanisms.MechanismError(
                        "native wrapper observation missed original deadline"
                    )
                time.sleep(0.02)
            startup = _read(directory / "startup.json")
            wrapper_proof = mechanisms._identity(
                proc.pid, ["/bin/bash", str(wrapper), "closer"], deadline=hard - 5
            )
            if (
                startup.get("wrapper") != _process_public(wrapper_proof)
                or startup.get("binding") != request["binding"]
            ):
                raise mechanisms.MechanismError(
                    "native wrapper observation does not match owned process"
                )
            entry = startup["entry"]
            independently = mechanisms._identity(
                entry["pid"], _entry_argv(manifest, manifest_path), deadline=hard - 5
            )
            if entry != _process_public(independently) or entry["pid"] == proc.pid:
                raise mechanisms.MechanismError("native entry process observation differs")
            chain = mechanisms._lineage(entry["pid"], deadline=hard - 5)
            if not any(
                row["pid"] == proc.pid and row["started"] == wrapper_proof["started"]
                for row in chain
            ):
                raise mechanisms.MechanismError("native entry is outside owned wrapper lineage")
            _write(directory / "observer-ack.json", {"nonce": nonce})
            stdout, stderr = proc.communicate(timeout=max(0.001, hard - time.monotonic()))
            signed = _read(directory / "terminal.json")
            record = signed["record"]
            if (
                proc.returncode != 0
                or record.get("passed") is not True
                or record.get("binding") != request["binding"]
            ):
                raise mechanisms.MechanismError("native no-effect lifecycle did not complete")
            if not hmac.compare_digest(
                signed["signature"], mechanisms._signature(record, mechanisms._key(root))
            ):
                raise mechanisms.MechanismError("native lifecycle signature differs")
            _write(root / "native-qualification.json", signed)
            return record
        except BaseException:
            # No fallback to legacy wrapper/provider and no second submission.
            # Its known no-effect children share the original action deadline.
            if proc.poll() is None:
                try:
                    proc.communicate(timeout=max(0.001, hard - time.monotonic()))
                except subprocess.TimeoutExpired:
                    pass
            raise


def _qualification_source_identity(manifest):
    """Same-call mutation sensitivity; never stores validation permission."""
    value = manifest["mechanism_contract"]
    names = [
        value["wrapper"]["path"],
        value["interpreter"]["path"],
        *(manifest.get("policy", {}).get("toml_sha256") or {}),
    ]
    files = tuple(
        (
            name,
            tuple(
                getattr(info, key)
                for key in (
                    "st_dev",
                    "st_ino",
                    "st_mode",
                    "st_nlink",
                    "st_uid",
                    "st_size",
                    "st_mtime_ns",
                    "st_ctime_ns",
                )
            ),
        )
        for name in names
        for info in [os.lstat(name)]
    )
    parents = sorted({str(parent) for name in names for parent in Path(name).parents})
    ancestry = tuple(
        (name, info.st_dev, info.st_ino, info.st_mode, info.st_uid)
        for name in parents
        for info in [os.lstat(name)]
    )
    return files, ancestry


def _qualified_receipt_root(manifest, *, _scope=None):
    """One authenticated local contract plus fresh signer/lifecycle evidence."""
    physical = _qualification_source_identity(manifest)
    _value, _wrapper, _python, root = contract(manifest, _scope=_scope)
    signed = _read(root / "native-qualification.json")
    record = signed["record"]
    if not hmac.compare_digest(
        signed["signature"], mechanisms._signature(record, mechanisms._key(root))
    ):
        raise mechanisms.MechanismError("native observer signature differs")
    if (
        record.get("schema") != OBSERVATION
        or record.get("kind") != "native-wrapper-qualification"
        or record.get("binding") != _binding_from_contract(manifest, _value)
        or record.get("passed") is not True
        or record.get("desktop_qualified") is not False
        or record.get("source") != "owned-native-wrapper-process"
        or record.get("actor") != "cursor"
        or record.get("scheduler_origin") != "unobserved"
    ):
        raise mechanisms.MechanismError("native lifecycle belongs to another capability or pin")
    directory = root / record["nonce"]
    if directory.parent != root or directory.is_symlink():
        raise mechanisms.MechanismError("native observation artifact path escaped")
    expected = {
        "startup": "startup.json",
        "handoff": "child-journal.json",
        "crash_restart": "crash.json",
        "result_recording": "child-result.json",
    }
    for phase, name in expected.items():
        ref = record["phases"][phase]
        if ref != {"file": name, "sha256": _hash(directory / name)}:
            raise mechanisms.MechanismError("native phase artifact changed: " + phase)
    result = _read(directory / "child-result.json")
    if (
        result.get("binding") != record["binding"]
        or result.get("nonce") != record["nonce"]
        or result.get("no_effect") is not True
        or _read(directory / "crash.json").get("exit") != -signal.SIGKILL
    ):
        raise mechanisms.MechanismError("native result belongs to another package or lifecycle")
    if physical != _qualification_source_identity(manifest):
        raise mechanisms.MechanismError("native qualification source changed during read")
    return root, physical


def qualification_receipt_problems(manifest, *, _scope=None):
    """Authenticate immutable package/lifecycle evidence; grants no ACT eligibility."""
    try:
        _qualified_receipt_root(manifest, _scope=_scope)
        return []
    except (OSError, ValueError, KeyError, TypeError, mechanisms.MechanismError) as exc:
        return [f"native handoff qualification held: {exc}"]


def qualification_problems(manifest, *, _scope=None):
    """ACT qualification retains the original current-entry/unknown barrier."""
    try:
        root, physical = _qualified_receipt_root(manifest, _scope=_scope)
        active = _read(root / "native-active.json")
        if active.get("status") != "terminal" and _CURRENT_ENTRY.get(str(root)) != active.get(
            "nonce"
        ):
            raise mechanisms.MechanismError("native submission remains unknown")
        if physical != _qualification_source_identity(manifest):
            raise mechanisms.MechanismError("native qualification source changed during read")
        return []
    except (OSError, ValueError, KeyError, TypeError, mechanisms.MechanismError) as exc:
        return [f"native handoff qualification held: {exc}"]


def status(manifest):
    _value, _wrapper, _python, root = contract(manifest)
    active = _read(root / "native-active.json") if (root / "native-active.json").exists() else None
    problems = qualification_problems(manifest)
    return {
        "mechanism": mechanisms.NATIVE,
        "source": "owned-native-wrapper-process",
        "desktop_qualified": False,
        "scheduler_origin": "unobserved",
        "qualified": not problems,
        "problems": problems,
        "active": active,
        "binding": binding(manifest),
    }


def _selftest():
    """Pure contract controls; never inspect or invoke a host wrapper."""
    source = '#!/bin/bash\nLANE="${1:?}"\ncase "$LANE" in\ncloser|opener) ;;\nesac\nexport PATH="$HOME/.local/bin:$HOME/.cursor/bin:$PATH"\nlegacy_provider\n'
    kwargs = dict(
        manifest_path="/private/pin.json",
        campaign_root="/private/campaigns",
        interpreter="/private/python",
        launcher="/private/closer_launcher.py",
    )
    rendered = render_cursor_wrapper(source, **kwargs)
    assert (
        rendered.index(MARKER) < rendered.index("export PATH") < rendered.index("legacy_provider")
    )
    assert 'exit "$?"' in rendered and rendered.count(MARKER) == 1
    try:
        render_cursor_wrapper(rendered, **kwargs)
    except mechanisms.MechanismError:
        pass
    else:
        raise AssertionError("double native entry admitted")
    assert policy_problem({})
    try:
        contract({})
    except mechanisms.MechanismError:
        pass
    else:
        raise AssertionError("unbound native manifest admitted")
    key = b"x" * 32
    assert mechanisms._signature({"target": "owner/repo#1"}, key) != mechanisms._signature(
        {"target": "owner/repo#2"}, key
    )
    print("closer_native_observer selftest: PASS (offline; no host/provider/queue actions)")
    return 0


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["--selftest"]:
        raise SystemExit(_selftest())
    raise SystemExit("Use the pinned closer_launcher native entrypoint")
