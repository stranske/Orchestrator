#!/usr/bin/env python3
"""Offline, read-only adapter to existing Closer efficiency accounting.

Input is retained receipts plus optional stage events, holds and independent
labels. This neither dispatches providers nor reads the live Brain or settings.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import resource
import sys
import time
from pathlib import Path

_root = Path(__file__).resolve().parents[1]
_modules = _root / "src"
if not (_modules / "closer_stage_feedback.py").is_file():
    _modules = _root
if not (_modules / "closer_stage_feedback.py").is_file():
    raise SystemExit("expected source or flat Closer measurement modules are missing")
sys.path.insert(0, str(_modules))
summarize_efficiency = importlib.import_module("closer_stage_feedback").summarize_efficiency


def measure(document):
    if not isinstance(document, dict):
        raise ValueError("acceptance workload must be an object")
    if document.get("schema") != "closer-acceptance-workload/v1":
        raise ValueError("unsupported acceptance workload")
    if document.get("evidence_kind") not in {"synthetic_fixture", "retained_observation"}:
        raise ValueError("explicit evidence kind required")
    started, cpu = time.perf_counter(), time.process_time()
    summary = summarize_efficiency(
        document["receipts"],
        stage_events=document.get("stage_events", []),
        holds=document.get("holds", []),
        independent_outcomes=document.get("independent_outcomes", []),
        current_bindings=document.get("current_bindings"),
        now=document["now"],
    )
    return {
        "schema": "closer-acceptance-measurement/v1",
        "evidence_kind": document["evidence_kind"],
        "workload_sha256": hashlib.sha256(
            json.dumps(document, sort_keys=True, allow_nan=False).encode()
        ).hexdigest(),
        "summary": summary,
        "measurement_plumbing": {
            "wall_seconds": time.perf_counter() - started,
            "cpu_seconds": time.process_time() - cpu,
            "process_peak_rss_platform_units": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "scope": "local summary computation only; RSS is process peak, not provider cost",
        },
        "baseline": {
            "status": "not_established",
            "live_savings_usd": None,
            "live_model_call_avoidance": None,
            "required": "paired exact-version installed observations with same task/criteria and independent outcome labels",
        },
        "remaining_measurements": {
            "repeat_classification": "needs evidence-identity/semantic dispatch ledger join",
            "missed_wake": "needs scheduled-due versus actual consumer invocation receipts; due is not missed",
            "escalation_acknowledgment": "requires installed delivery/ack-window receipts joined to the exact hold",
            "provider_quota_pressure": "requires measured provider/account records",
        },
        "authority": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    def reject_constant(name):
        raise ValueError(f"non-finite JSON constant {name} is not allowed")

    try:
        if args.input.stat().st_size > 8 * 1024 * 1024:
            parser.error("workload exceeds 8 MiB")
        result = measure(
            json.loads(args.input.read_text(encoding="utf-8"), parse_constant=reject_constant)
        )
        encoded = json.dumps(result, indent=2, allow_nan=False) + "\n"
    except (KeyError, ValueError, TypeError, OSError) as exc:
        parser.error(f"invalid workload: {exc}")
    if args.output:
        args.output.write_text(encoded)
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
