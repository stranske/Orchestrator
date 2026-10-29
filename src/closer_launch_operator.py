#!/usr/bin/env python3
"""Thin operator interface for the existing pinned native closer path.

Planning is read-only.  ``launch`` is explicit, re-runs the business-capacity
diagnostic, and then invokes only the manifest's canonical ``native-entry``
argv; it never swaps a wrapper, clears a campaign, or manufactures admission.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def _load_manifest(path: Path):
    from closer_campaign import load_manifest

    value = Path(path)
    if not value.is_absolute() or value.is_symlink() or value.resolve() != value:
        raise ValueError("manifest must be a canonical absolute non-symlink path")
    return load_manifest(value)


def campaign_gate(campaign_path: Path, manifest_path: Path) -> dict[str, Any]:
    """Describe the finite retry boundary without mutating a cohort."""
    from closer_campaign import Campaign

    document = Campaign(campaign_path).read()
    if not isinstance(document, dict):
        return {"stage": "held", "reason": "campaign is absent or unreadable"}
    cfg = document.get("worker_config") or {}
    if cfg.get("runtime_manifest") != str(manifest_path):
        return {"stage": "held", "reason": "campaign does not bind this exact manifest"}
    if cfg.get("finite_frontier") is None:
        return {"stage": "ready", "kind": "non-finite"}
    unavailable = [
        worker.get("worker_id")
        for worker in document.get("workers", [])
        if worker.get("state") == "terminal" and worker.get("outcome") == "unavailable"
    ]
    if unavailable or document.get("targets", {}).get("*round*") == "reconcile":
        return {
            "stage": "held",
            "kind": "finite-disposition-required",
            "workers": unavailable,
            "reason": "finite unavailable is one attempt only; completed-cohort retirement is inapplicable until a new authenticated disposition preserves the old result",
            "accepted_dispositions": [
                "native-pre-dispatch-no-start/v2",
                "native-retained-no-b-reconciliation/v1",
            ],
        }
    return {"stage": "ready", "kind": "finite-current-authority"}


def plan(
    manifest_path: Path,
    campaign_path: Path,
    run_id: str,
    *,
    refresh_capacity_owner: bool = False,
) -> dict[str, Any]:
    import closer_launch_preflight as readiness
    import closer_native_observer as native

    if not run_id or len(run_id) > 160:
        raise ValueError("run identity is required and bounded")
    manifest = _load_manifest(manifest_path)
    # This independently validates the pinned native contract before exposing
    # argv.  It does not issue entry, admission, or effect authority.
    native.contract(manifest)
    report = readiness.inspect(manifest_path, refresh_capacity_owner=refresh_capacity_owner)
    campaign = campaign_gate(campaign_path, manifest_path)
    return {
        "schema": "closer-launch-operator-plan/v1",
        "run_id": run_id,
        "authority": False,
        "manifest": report.get("manifest"),
        "business_capacity": report,
        "campaign": {"path": str(campaign_path), **campaign},
        "canonical_native_entry": native._entry_argv(manifest, manifest_path),
        "launch_permitted_by_diagnostic": report.get("stage") == "ready"
        and campaign.get("stage") == "ready",
        "note": "The source-owned native entry re-observes locks, qualification, admission, and effects.",
    }


def acceptance_ledger(
    campaign_path: Path, result_path: Path, native_terminal: Path
) -> dict[str, Any]:
    """Report retained evidence only; never infer coding/publication from green CI."""
    from closer_campaign import Campaign

    campaign = Campaign(campaign_path).read()
    result = json.loads(result_path.read_text())
    terminal = json.loads(native_terminal.read_text())
    detail = json.loads(result.get("detail", "{}")) if isinstance(result, dict) else {}
    execution = detail.get("native_execution") if isinstance(detail, dict) else {}
    phases = execution.get("phases") if isinstance(execution, dict) else {}
    phase_a = phases.get("A") if isinstance(phases, dict) else None
    phase_b = phases.get("B") if isinstance(phases, dict) else None
    rows = [
        {"stage": "goal", "state": "proven", "evidence": campaign.get("campaign_id")},
        {
            "stage": "ACTION A",
            "state": "observed" if isinstance(phase_a, dict) else "UNKNOWN",
            "evidence": phase_a,
        },
        {
            "stage": "ACTION B",
            "state": "observed" if isinstance(phase_b, dict) else "UNKNOWN",
            "evidence": phase_b,
        },
        {"stage": "coding", "state": "UNKNOWN", "evidence": None},
        {"stage": "publication", "state": "UNKNOWN", "evidence": None},
        {"stage": "Verify", "state": "UNKNOWN", "evidence": None},
        {"stage": "acceptance", "state": "UNKNOWN", "evidence": None},
    ]
    if isinstance(detail, dict) and detail.get("descendant_bundle"):
        rows[3] = {"stage": "coding", "state": "observed", "evidence": detail["descendant_bundle"]}
    if isinstance(terminal, dict):
        rows[2]["terminal_native_observation"] = terminal.get("kind")
    last = next((row for row in reversed(rows) if row["state"] != "UNKNOWN"), rows[0])
    return {
        "schema": "closer-launch-acceptance-ledger/v1",
        "authority": False,
        "rows": rows,
        "last_proven_boundary": {"stage": last["stage"], "state": last["state"]},
        "warning": "No coding, publication, Verify, or acceptance claim is derived from release/CI color.",
    }


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--selftest"]:
        with tempfile.TemporaryDirectory() as directory:
            assert campaign_gate(Path(directory) / "missing", Path("/manifest"))["stage"] == "held"
        print("closer_launch_operator selftest: PASS (no launch)")
        return 0
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "launch"):
        command = sub.add_parser(name)
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--campaign", type=Path, required=True)
        command.add_argument("--run-id", required=True)
        command.add_argument("--refresh-capacity-owner", action="store_true")
    sub.choices["launch"].add_argument("--confirm-run-id")
    ledger = sub.add_parser("ledger")
    ledger.add_argument("--campaign", type=Path, required=True)
    ledger.add_argument("--result", type=Path, required=True)
    ledger.add_argument("--native-terminal", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "ledger":
        print(
            json.dumps(
                acceptance_ledger(args.campaign, args.result, args.native_terminal), sort_keys=True
            )
        )
        return 0
    prepared = plan(
        args.manifest,
        args.campaign,
        args.run_id,
        refresh_capacity_owner=args.refresh_capacity_owner,
    )
    if args.command == "plan":
        print(json.dumps(prepared, sort_keys=True))
        return 0
    if args.confirm_run_id != args.run_id:
        parser.error("launch requires --confirm-run-id matching --run-id")
    if not prepared["launch_permitted_by_diagnostic"]:
        print(json.dumps(prepared, sort_keys=True))
        return 75
    # Do not wrap, replace, or augment the reviewed argv.  Native entry owns
    # all fresh admission and actual-effect gates.
    completed = subprocess.run(prepared["canonical_native_entry"], check=False)
    prepared["native_entry_exit"] = completed.returncode
    print(json.dumps(prepared, sort_keys=True))
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
