"""Pinned native-wrapper handoff observation, never Desktop scheduler attestation.

The reviewed wrapper and this fixed observer are trusted same-UID host code. A
signature detects substituted JSON; it is not an OS boundary against that user.
No provider, queue or scheduler-control API is used by qualification.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
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


def contract(manifest):
    value = manifest.get("mechanism_contract") or {}
    if value.get("schema") != mechanisms.CONTRACT or value.get("mechanism") != mechanisms.NATIVE:
        raise mechanisms.MechanismError("native mechanism contract missing")
    # Concrete supported wrapper. Other actors remain independent capabilities.
    wrapper = Path.home() / ".codex/bin/cursor-lane.sh"
    pin = value.get("wrapper") or {}
    if value.get("actor") != "cursor" or pin.get("path") != str(wrapper):
        raise mechanisms.MechanismError("native wrapper/actor is not supported")
    if wrapper.is_symlink() or wrapper.resolve() != wrapper or _hash(wrapper) != pin.get("sha256"):
        raise mechanisms.MechanismError("native wrapper pin changed or aliased")
    interpreter = Path(value.get("interpreter", {}).get("path", ""))
    if (
        interpreter.resolve() != interpreter
        or not interpreter.is_file()
        or _hash(interpreter) != value.get("interpreter", {}).get("sha256")
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
        validate_admission(manifest, context, for_effects=True)
        return context
    directory = root / nonce
    assignment_source = (
        Path.home()
        / ".codex/automations/reviewed-repo-automation-controller/closer-assignment.json"
    )
    demand = doc.get("demand_admission")
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


def validate_admission(manifest, context, *, for_effects):
    import closer_operational_capacity as scoped

    policy = capacity_contract(manifest, control_plane_only=True)
    root = contract(manifest)[3]
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
        signed.get("record") != {"binding": binding(manifest), "context": context}
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
    source = (
        Path.home()
        / ".codex/automations/reviewed-repo-automation-controller/closer-assignment.json"
    )
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
    if "demand_assignment" in context:
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
        if context.get("schema") != "native-invocation-admission/v1":
            return None  # no legacy deadline proof can authorize a new frontier
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
            or not context["started_at"]
            <= times[0]
            <= times[1]
            <= times[2]
            <= times[3]
            <= times[4]
            <= context["original_deadline"]
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
            "schema": "native-pre-dispatch-no-start/v1",
            "worker": fields,
            "admission_nonce": context["nonce"],
            "assignment_sha256": context["assignment"]["sha256"],
            "original_deadline": context["original_deadline"],
            "phases": phases,
            "execution": execution,
            "accepted": False,
        }
        boundary = directory / ("pre-dispatch-no-start-" + worker_id + ".json")
        record = {"binding": binding(manifest), "proof": proof}
        if record_boundary:
            observed_now = time.time()
            if (
                not math.isfinite(observed_now)
                or _CURRENT_ENTRY.get(str(root)) != context["nonce"]
                or not times[4] <= observed_now < context["original_deadline"]
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


def binding(manifest):
    value, _wrapper, _python, _root = contract(manifest)
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


def entry_active(manifest):
    """Only the observed, lock-held entry in this process may schedule work."""
    _value, _wrapper, _python, root = contract(manifest)
    active = _read(root / "native-active.json")
    return (
        active.get("binding") == binding(manifest)
        and active.get("status") == "submitted"
        and _CURRENT_ENTRY.get(str(root)) == active.get("nonce")
    )


def authorize_worker(manifest, argv):
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


def qualification_receipt_problems(manifest):
    """Authenticate immutable package/lifecycle evidence; grants no ACT eligibility."""
    try:
        _value, _wrapper, _python, root = contract(manifest)
        signed = _read(root / "native-qualification.json")
        record = signed["record"]
        if not hmac.compare_digest(
            signed["signature"], mechanisms._signature(record, mechanisms._key(root))
        ):
            raise mechanisms.MechanismError("native observer signature differs")
        if (
            record.get("schema") != OBSERVATION
            or record.get("kind") != "native-wrapper-qualification"
            or record.get("binding") != binding(manifest)
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
        return []
    except (OSError, ValueError, KeyError, TypeError, mechanisms.MechanismError) as exc:
        return [f"native handoff qualification held: {exc}"]


def qualification_problems(manifest):
    """ACT qualification retains the original current-entry/unknown barrier."""
    problems = qualification_receipt_problems(manifest)
    if problems:
        return problems
    try:
        _value, _wrapper, _python, root = contract(manifest)
        active = _read(root / "native-active.json")
        if active.get("status") != "terminal" and _CURRENT_ENTRY.get(str(root)) != active.get(
            "nonce"
        ):
            raise mechanisms.MechanismError("native submission remains unknown")
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
