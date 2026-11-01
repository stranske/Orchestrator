#!/usr/bin/env python3
"""Scoped operational capacity decisions; never scans sessions or clears holds.

A current evaluation is not a fresh quota measurement. Unknown quota permits
only an explicitly bounded 429-authoritative trial, never inferred headroom.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, TypeGuard

import capacity
import execution_profiles
import pacing_usage

SCHEMA = "closer-operational-capacity/v1"
AGENTS = ("codex", "claude", "gemini", "muse", "cursor", "vibe")
MAX_BYTES = 256_000


class CapacityError(ValueError):
    pass


class _AdmittedCapacityReport(dict):
    """A report whose path, bytes, and signed native admission are rechecked on use."""

    _admitted_path: str
    _admitted_sha256: str


def _admitted_report(report: dict, *, path: Path, sha256: str) -> dict:
    value = _AdmittedCapacityReport(report)
    value._admitted_path = str(path)
    value._admitted_sha256 = sha256
    return value


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _raw(path: Path) -> bytes | None:
    if path.is_symlink() or path.resolve() != path:
        raise CapacityError("operational input symlink refused")
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(raw) > MAX_BYTES:
        raise CapacityError("operational input exceeds bound")
    return raw


def _read(path: Path) -> tuple[dict, str | None]:
    raw = _raw(path)
    if raw is None:
        return {}, None
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise CapacityError("operational input must be an object")
    return value, _sha(raw)


def _scoped_input_paths(profile_ids) -> tuple[Path, ...] | None:
    """Return the host inputs that can affect this exact profile scope.

    A scoped report is not a fleet-wide snapshot.  Requiring a write to an
    unrelated provider's usage poll to remain byte-identical makes a valid
    selected pool disappear between admission and dispatch.  Keep the input
    fence closed over every selected agent (and therefore every selected
    profile/pool), while leaving unselected operational observations outside
    the report's freshness contract.
    """
    if not isinstance(profile_ids, list) or not profile_ids:
        return None
    selected = set()
    for profile_id in profile_ids:
        profile = execution_profiles.get_profile(profile_id)
        if (
            not isinstance(profile, dict)
            or profile.get("agent") not in AGENTS
            or profile.get("lifecycle_status") != "active"
        ):
            return None
        selected.add(profile["agent"])
    polls = Path.home() / ".codex/orchestrator/usage-poll"
    sheds = Path.home() / ".codex/handoff/capacity-shed"
    paths = [sheds / agent for agent in sorted(selected)]
    paths.extend(polls / f"{agent}-usage.json" for agent in sorted(selected & {"codex", "claude"}))
    return tuple(paths)


def _operational_inputs_match(inputs, profile_ids) -> bool:
    expected = _scoped_input_paths(profile_ids)
    if (
        expected is None
        or not isinstance(inputs, dict)
        or set(inputs) != {str(path) for path in expected}
    ):
        return False
    try:
        for path in expected:
            raw = _raw(path)
            if inputs[str(path)] != (_sha(raw) if raw is not None else None):
                return False
        return True
    except (OSError, ValueError):
        return False


def _evaluate(config: dict, *, now: float | None = None, admitted_profile_ids=None) -> dict:
    now = time.time() if now is None else now
    deadline = config.get("original_deadline")
    if config.get("schema") != SCHEMA or not _number(deadline) or not now < deadline <= now + 900:
        raise CapacityError("original bounded round deadline required (maximum 900 seconds)")
    owner = config.get("owner")
    if not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", owner):
        raise CapacityError("explicit publication owner required")
    requested = config.get("profile_ids") if admitted_profile_ids is None else admitted_profile_ids
    control_plane_only = config.get("control_plane_only", False)
    if type(control_plane_only) is not bool:
        raise CapacityError("control-plane qualification must be explicit")
    if (
        not isinstance(requested, list)
        or len(set(requested)) != len(requested)
        or (control_plane_only and requested)
        or (not control_plane_only and not requested)
    ):
        raise CapacityError("exact model scope or explicit empty control-plane scope required")
    for key in requested:
        row = execution_profiles.get_profile(key)
        if not row or row.get("agent") not in AGENTS or row.get("lifecycle_status") != "active":
            raise CapacityError("unknown or inactive scoped profile")
    selected_agents = {execution_profiles.get_profile(key)["agent"] for key in requested}
    trials = config.get("bounded_trial_agents", [])
    if not isinstance(trials, list) or set(trials) - set(AGENTS) or (control_plane_only and trials):
        raise CapacityError("unsupported bounded trial policy")
    # These fixed operational paths are host-local, never public package constants.
    poll_dir = Path.home() / ".codex/orchestrator/usage-poll"
    shed_dir = Path.home() / ".codex/handoff/capacity-shed"
    for directory in (() if control_plane_only else (poll_dir, shed_dir)):
        if directory.resolve() != directory or directory.is_symlink():
            raise CapacityError("canonical operational directory required")
    manifest = Path(config.get("runtime_manifest", ""))
    pin, digest = _read(manifest)
    if not digest or digest != config.get("runtime_manifest_sha256"):
        raise CapacityError("runtime manifest physical hash mismatch")
    modules = pin.get("modules") or {}
    module = modules.get(Path(__file__).name)
    expected = module.get("sha256") if isinstance(module, dict) else module
    if expected != _sha(Path(__file__).read_bytes()):
        raise CapacityError("producer is outside pinned Runtime closure")
    agents: dict[str, Any] = {}
    inputs: dict[str, str | None] = {}
    observed_agents = (
        () if control_plane_only else (selected_agents if admitted_profile_ids else AGENTS)
    )
    for agent in observed_agents:
        marker_path = shed_dir / agent
        if marker_path.is_symlink():
            raise CapacityError("shed marker symlink refused")
        before = _raw(marker_path)
        marker = capacity.shed_marker(agent, now=now, shed_dir=shed_dir)
        after = _raw(marker_path)
        if before != after:
            raise CapacityError("shed marker changed during decision")
        if agent in selected_agents:
            inputs[str(marker_path)] = _sha(before) if before is not None else None
        model = capacity.AGENTS[agent].get("model")
        policy_trial = model in {"metered", "flat"} or (
            model == "ccusage" and capacity.AGENTS[agent].get("block_token_limit") is None
        )
        trial = agent in trials and policy_trial
        state, reason = "unknown", "remaining quota unknown; no scoped trial permission"
        observation = None
        raw: dict[str, Any] = {}
        if agent in {"codex", "claude"}:
            path = poll_dir / f"{agent}-usage.json"
            raw, input_hash = _read(path)
            if agent in selected_agents:
                inputs[str(path)] = input_hash
            observation = pacing_usage.polled_usage(agent, now, data=raw)
            poll = observation["poll"]
            used = observation.get("used_fraction")
            short = observation.get("short_used_fraction")
            weekly = raw.get("primary" if agent == "codex" else "weekly") or {}
            short_window = raw.get("secondary" if agent == "codex" else "session") or {}
            consistent = (
                raw.get("source") == observation["source"]
                and _number(raw.get("ts"))
                and raw["ts"] <= now
                and all(
                    isinstance(window, dict)
                    and _number(window.get("used_percent"))
                    and 0 <= window["used_percent"] <= 100
                    and _number(window.get("resets_at"))
                    for window in (weekly, short_window)
                )
            )
            denied = agent == "codex" and raw.get("ordinary_usage_allowed") is False
            exhausted = (
                used == 1
                or short == 1
                or any(
                    isinstance(window, dict)
                    and _number(window.get("used_percent"))
                    and window["used_percent"] >= 100
                    for window in (weekly, short_window)
                )
            )
            # Known restrictions stay authoritative even if the last poll is stale.
            if denied or exhausted:
                state, reason = "shed", "provider poll denies ordinary usage or reports exhaustion"
            elif (
                consistent
                and poll.get("fresh") is True
                and short_window["resets_at"] > now
                and (agent != "codex" or raw.get("ordinary_usage_allowed") is True)
                and _number(used)
                and observation.get("period_end", 0) > now
                and _number(short)
            ):
                state = "warn" if max(used, short) >= 0.8 else "ok"
                reason = "fresh operational provider metrics; auth checked separately at submission"
            elif (
                trial
                and (not raw or (consistent and poll.get("valid") is True))
                and (agent != "codex" or (consistent and raw.get("ordinary_usage_allowed") is True))
            ):
                state, reason = "warn", "bounded 429-authoritative trial; remaining quota unknown"
        elif trial:
            state, reason = "warn", "bounded 429-authoritative trial; remaining quota unknown"
        if state != "shed" and model == "ccusage" and not policy_trial:
            state, reason = (
                "unknown",
                "configured token ceiling requires unavailable usage evidence",
            )
        if marker is not None and marker["state"] != "expired":
            state, reason = "shed", f"read-only shed marker: {marker['state']}"
        agents[agent] = dict(
            state=state,
            reason=reason,
            remaining_quota="unknown",
            accounting_model=model,
            trial_policy_supported=policy_trial,
            observation=observation,
            shed_marker=marker,
            bounded_trial=state == "warn" and "trial" in reason,
        )
    projection = (
        {"profiles": {}, "pools": {}}
        if control_plane_only
        else capacity.profile_capacity_snapshot(
            {"agents": agents},
            registry={key: execution_profiles.get_profile(key) for key in requested},
        )
    )
    # No operational counter is a measurement of unused provider quota.
    projection["pools"] = {
        key: row for key, row in projection["pools"].items() if row.get("agent") in AGENTS
    }
    for pool in projection["pools"].values():
        pool["used"] = None
    return dict(
        schema=SCHEMA,
        generated_at=now,
        expires_at=min(deadline, now + 1800),
        original_deadline=deadline,
        owner=owner,
        runtime_manifest_sha256=digest,
        producer_sha256=_sha(Path(__file__).read_bytes()),
        profile_ids=requested,
        **({"control_plane_only": True} if control_plane_only else {}),
        input_sha256=inputs,
        agents=agents,
        **projection,
    )


def evaluate(config: dict, *, now: float | None = None) -> dict:
    """Produce a complete current report for strict generic consumers."""
    return _evaluate(config, now=now)


def _report_snapshot_valid(report: dict, *, now: float) -> bool:
    """Validate the immutable report snapshot apart from host inputs that may refresh."""
    if not isinstance(report, dict):
        return False
    deadline = report.get("original_deadline")
    return bool(
        report.get("schema") == SCHEMA
        and _number(deadline)
        and _number(report.get("generated_at"))
        and _number(report.get("expires_at"))
        and report["generated_at"] <= now + 30
        and now < report["expires_at"] <= deadline <= report["generated_at"] + 900
        and report.get("producer_sha256") == _sha(Path(__file__).read_bytes())
        and type(report.get("control_plane_only", False)) is bool
        and isinstance(report.get("profile_ids"), list)
        and (
            (
                report.get("control_plane_only") is True
                and not report["profile_ids"]
                and not report.get("profiles")
                and not report.get("pools")
            )
            or (not report.get("control_plane_only", False) and bool(report["profile_ids"]))
        )
        and len(set(report["profile_ids"])) == len(report["profile_ids"])
        and set(report["profile_ids"]) == set(report.get("profiles", {}))
        and all(execution_profiles.get_profile(key) for key in report["profile_ids"])
    )


def validate_report(report: dict, *, now: float) -> bool:
    """Require byte-identical current inputs for generic report consumers."""
    return bool(
        _report_snapshot_valid(report, now=now)
        and (
            (
                report.get("control_plane_only") is True
                and report.get("input_sha256") == {}
                and report.get("agents") == {}
            )
            or (
                report.get("control_plane_only", False) is False
                and _operational_inputs_match(report.get("input_sha256"), report["profile_ids"])
            )
        )
    )


def validate_admitted_report(
    report: dict,
    *,
    now: float,
    policy: dict,
    runtime_manifest: str,
    runtime_manifest_sha256: str,
) -> bool:
    """Recheck an admitted model scope against current bounded provider facts.

    A signed admission keeps its original report bytes and scope.  A routine
    poll refresh must not invalidate that snapshot solely by changing a hash,
    but a newly observed hold, exhaustion, denial, or unusable metric remains
    fail-closed before work may continue.
    """
    if (
        not _report_snapshot_valid(report, now=now)
        or report.get("control_plane_only", False) is not False
        or not isinstance(policy, dict)
        or report.get("owner") != policy.get("owner")
        or report.get("profile_ids") != policy.get("profile_ids")
        or report.get("runtime_manifest_sha256") != runtime_manifest_sha256
    ):
        return False
    try:
        original_states = [
            report["profiles"][profile_id]["state"] for profile_id in report["profile_ids"]
        ]
        if not all(isinstance(state, str) for state in original_states):
            return False
        # An already quota-shed signed scope has no model admission to refresh.
        # Keep its existing byte-for-byte host-input fence; it can never acquire
        # a model merely because a later observation is more favorable.
        if all(state not in {"ok", "warn"} for state in original_states):
            return all(state == "shed" for state in original_states) and validate_report(
                report, now=now
            )
        admitted_profile_ids = [
            profile_id
            for profile_id, state in zip(report["profile_ids"], original_states, strict=True)
            if state in {"ok", "warn"}
        ]
        current = _evaluate(
            {
                "schema": SCHEMA,
                "owner": policy["owner"],
                "original_deadline": report["original_deadline"],
                "profile_ids": admitted_profile_ids,
                "bounded_trial_agents": policy["bounded_trial_agents"],
                "runtime_manifest": runtime_manifest,
                "runtime_manifest_sha256": runtime_manifest_sha256,
            },
            now=now,
            admitted_profile_ids=admitted_profile_ids,
        )
        selected = {
            execution_profiles.get_profile(profile_id)["agent"]
            for profile_id in admitted_profile_ids
        }
        return all(current["agents"][agent]["state"] in {"ok", "warn"} for agent in selected)
    except (CapacityError, KeyError, OSError, TypeError, ValueError):
        return False


def publish(config: dict, output: Path, *, expected_sha256: str | None) -> dict:
    """Distinct output only; lock+owner+CAS prevent competing scoped producers."""
    if not output.is_absolute() or output.name == "capacity.json" or output.resolve() != output:
        raise CapacityError("distinct canonical absolute scoped report path required")
    lock = output.with_name(output.name + ".lock")
    if lock.is_symlink():
        raise CapacityError("scoped report lock symlink refused")
    with lock.open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        prior, actual = _read(output)
        if actual != expected_sha256 or (actual and prior.get("owner") != config.get("owner")):
            raise CapacityError("scoped report owner or compare-and-swap conflict")
        report = evaluate(config)
        descriptor, temp = tempfile.mkstemp(prefix=".scoped-capacity-", dir=output.parent)
        try:
            with os.fdopen(descriptor, "w") as target:
                json.dump(report, target, sort_keys=True)
                target.write("\n")
                target.flush()
                os.fsync(target.fileno())
            os.replace(temp, output)
            directory = os.open(output.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            readback, _ = _read(output)
            if readback != report:
                raise CapacityError("scoped report readback mismatch")
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        return report


def declared_profile_ids(report: dict) -> tuple[str, ...] | None:
    """Return a current native report's closed scope, or ``None`` for global reports.

    A malformed or stale object that merely claims this schema returns an empty
    scope.  That preserves the ordinary global route while failing closed for a
    claimed scoped route.
    """
    if not isinstance(report, dict) or report.get("schema") != SCHEMA:
        return None
    try:
        if isinstance(report, _AdmittedCapacityReport):
            try:
                import closer_native_observer as native

                if not native.revalidate_admitted_capacity_report(report):
                    return ()
            except (KeyError, TypeError, ValueError, OSError, OverflowError):
                return ()
        elif not validate_report(report, now=time.time()):
            return ()
    except (KeyError, TypeError, ValueError, OSError, OverflowError):
        return ()
    return tuple(report["profile_ids"])


def selftest() -> None:
    """Offline invariant checks with disposable operational input paths only."""
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        prior = os.environ.get("HOME")
        os.environ["HOME"] = str(Path(directory).resolve())
        try:
            root = Path(directory).resolve()
            manifest = root / "runtime.json"
            manifest.write_text(
                json.dumps({"modules": {Path(__file__).name: _sha(Path(__file__).read_bytes())}})
            )
            config = dict(
                schema=SCHEMA,
                owner="offline-selftest",
                original_deadline=1600,
                profile_ids=["claude-sonnet-5-5-low"],
                runtime_manifest=str(manifest),
                runtime_manifest_sha256=_sha(manifest.read_bytes()),
                bounded_trial_agents=["claude"],
            )
            report = evaluate(config, now=1000)
            assert report["agents"]["claude"]["state"] == "warn"
            assert report["agents"]["codex"]["state"] == "unknown"
            assert validate_report(report, now=1000)
            assert not validate_report(report, now=1601)
        finally:
            if prior is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = prior
    print("closer_operational_capacity selftest: PASS (offline)")


def main(argv=None) -> int:
    if (sys.argv[1:] if argv is None else argv) == ["--selftest"]:
        selftest()
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True, help="exact prior hash, or absent")
    args = parser.parse_args(argv)
    try:
        config, _ = _read(args.config)
        report = publish(
            config,
            args.output,
            expected_sha256=None if args.expected_sha256 == "absent" else args.expected_sha256,
        )
        print(json.dumps(report, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}))
        return 78


if __name__ == "__main__":
    sys.exit(main())
