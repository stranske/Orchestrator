#!/usr/bin/env python3
"""Read native business-launch readiness without creating an admission or worker.

This is an operator diagnostic only.  ``ready`` means the current evidence is
usable for a *later* guarded entry; it never grants launch, model, admission,
or effect authority.  The entry point re-observes every gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _marker(capacity_module, agent: str, *, now: float) -> dict[str, Any] | None:
    try:
        return capacity_module.shed_marker(agent, now=now)
    except (OSError, ValueError, TypeError):
        return {"agent": agent, "state": "unreadable"}


def _refresh_selected_sheds(
    capacity_module, agents: set[str], *, now: float
) -> list[dict[str, Any]]:
    """Run only capacity's conditional owner recovery for selected agents."""
    rows = []
    for agent in sorted(agents):
        before = _marker(capacity_module, agent, now=now)
        held = capacity_module._shed(agent)
        after = _marker(capacity_module, agent, now=time.time())
        rows.append({"agent": agent, "before": before, "held": held, "after": after})
    return rows


def _report_reasons(report: dict[str, Any], profile_ids: list[str]) -> list[str]:
    reasons: list[str] = []
    profile_rows, agent_rows, pool_rows = (
        report.get("profiles"),
        report.get("agents"),
        report.get("pools"),
    )
    profiles: dict[str, Any] = profile_rows if isinstance(profile_rows, dict) else {}
    agents: dict[str, Any] = agent_rows if isinstance(agent_rows, dict) else {}
    pools: dict[str, Any] = pool_rows if isinstance(pool_rows, dict) else {}
    for profile_id in profile_ids:
        profile = profiles.get(profile_id)
        if not isinstance(profile, dict) or profile.get("state") not in {"ok", "warn"}:
            reasons.append(f"profile {profile_id} is not operationally usable")
            continue
        pool_ids = profile.get("capacity_pool_ids")
        if not isinstance(pool_ids, list) or not pool_ids:
            reasons.append(f"profile {profile_id} has no declared capacity pools")
            continue
        for pool_id in pool_ids:
            pool = pools.get(pool_id)
            if not isinstance(pool, dict) or pool.get("state") not in {"ok", "warn"}:
                reasons.append(f"profile {profile_id} pool {pool_id} is not operationally usable")
                continue
            agent = pool.get("agent")
            row = agents.get(agent) if isinstance(agent, str) else None
            if (
                not isinstance(agent, str)
                or not isinstance(row, dict)
                or row.get("state")
                not in {
                    "ok",
                    "warn",
                }
            ):
                reasons.append(f"profile {profile_id} agent {agent!r} is not operationally usable")
    return reasons


def inspect(manifest_path: str | Path, *, refresh_capacity_owner: bool = False) -> dict[str, Any]:
    """Return a fail-closed readiness report; default mode never writes host state."""
    import capacity
    import closer_native_observer as native
    import closer_operational_capacity as operational
    import execution_profiles
    from closer_campaign import load_manifest

    supplied_manifest = Path(manifest_path)
    if (
        not supplied_manifest.is_absolute()
        or supplied_manifest.is_symlink()
        or supplied_manifest.resolve() != supplied_manifest
    ):
        return {
            "schema": "closer-business-launch-preflight/v1",
            "authority": False,
            "stage": "held",
            "manifest": {"path": str(supplied_manifest)},
            "refresh_capacity_owner": refresh_capacity_owner,
            "reasons": ["closure: manifest path is not canonical"],
        }
    manifest_file = supplied_manifest
    result: dict[str, Any] = {
        "schema": "closer-business-launch-preflight/v1",
        "authority": False,
        "stage": "held",
        "manifest": {"path": str(manifest_file)},
        "refresh_capacity_owner": refresh_capacity_owner,
        "reasons": [],
    }
    try:
        manifest = load_manifest(manifest_file)
        evaluator = Path(__file__).resolve()
        evaluator_sha256 = _sha256(evaluator)
        if manifest.get("modules", {}).get(evaluator.name) != evaluator_sha256:
            raise ValueError("readiness evaluator is outside the pinned Runtime closure")
        result["evaluator"] = {"path": str(evaluator), "sha256": evaluator_sha256}
        # contract verifies the supported wrapper/interpreter/root pins; loading
        # verifies every declared Runtime module before capacity is consulted.
        _contract, _wrapper, _interpreter, _root = native.contract(manifest)
        policy = native.capacity_contract(manifest, control_plane_only=True)
        if policy is None:
            raise ValueError("native capacity publication is not configured")
        profile_ids = list(policy["profile_ids"])
        result["manifest"]["sha256"] = _sha256(manifest_file)
        result["manifest"]["package_digest"] = manifest.get("package_digest")
        result["profile_ids"] = profile_ids
        result["producer"] = {
            "path": str(Path(operational.__file__).resolve()),
            "sha256": _sha256(Path(operational.__file__).resolve()),
        }
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        result["reasons"].append(f"closure: {type(exc).__name__}: {exc}")
        return result

    # This is deliberately separate from the control-plane parse above.  A
    # control-only contract is never evidence that a profile can do business.
    try:
        native.capacity_contract(manifest)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        result["reasons"].append(f"qualification: {type(exc).__name__}: {exc}")
        return result

    selected_agents: set[str] = set()
    for profile_id in profile_ids:
        profile = execution_profiles.get_profile(profile_id)
        agent = profile.get("agent") if isinstance(profile, dict) else None
        if isinstance(agent, str):
            selected_agents.add(agent)
    if refresh_capacity_owner:
        try:
            result["shed_recovery"] = _refresh_selected_sheds(
                capacity, selected_agents, now=time.time()
            )
        except (OSError, ValueError, RuntimeError, TypeError) as exc:
            result["reasons"].append(f"shed recovery: {type(exc).__name__}: {exc}")
            return result

    now = time.time()
    window = min(900, int(policy.get("freshness_seconds", policy.get("admission_seconds", 900))))
    config = {
        "schema": operational.SCHEMA,
        "owner": policy["owner"],
        "profile_ids": profile_ids,
        "bounded_trial_agents": list(policy["bounded_trial_agents"]),
        "original_deadline": now + window,
        "runtime_manifest": str(manifest_file),
        "runtime_manifest_sha256": result["manifest"]["sha256"],
    }
    try:
        report = operational.evaluate(config, now=now)
        result["capacity"] = {
            "generated_at": report.get("generated_at"),
            "expires_at": report.get("expires_at"),
            "input_sha256": report.get("input_sha256"),
        }
        if not operational.validate_report(report, now=time.time()):
            result["reasons"].append("capacity: report is stale or inputs changed during read")
            return result
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, OverflowError) as exc:
        result["reasons"].append(f"capacity: {type(exc).__name__}: {exc}")
        return result
    result["reasons"].extend(_report_reasons(report, profile_ids))
    if not result["reasons"]:
        result["stage"] = "ready"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--refresh-capacity-owner", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        report: dict[str, Any] = {
            "profiles": {"profile": {"state": "ok", "capacity_pool_ids": ["pool"]}},
            "pools": {"pool": {"state": "ok", "agent": "claude"}},
            "agents": {"claude": {"state": "ok"}},
        }
        assert _report_reasons(report, ["profile"]) == []
        report["agents"]["claude"]["state"] = "shed"
        assert _report_reasons(report, ["profile"])
        return 0
    if args.manifest is None:
        parser.error("--manifest is required unless --selftest is used")
    result = inspect(args.manifest, refresh_capacity_owner=args.refresh_capacity_owner)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["stage"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
