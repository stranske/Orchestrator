"""One actual inert closed-issue native receipt -> authenticated metrics join.

This is synthetic local composition evidence, never a live BEFORE baseline.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest
from test_closer_campaign_demand_integration import TARGET
from test_closer_campaign_demand_integration import host as host
from test_closer_campaign_demand_integration import publisher as publisher
from test_closer_completed_cohort_reuse import _completed_worker
from test_closer_completed_cohort_reuse import completed_native_world as completed_native_world

import closer_campaign as campaign
import closer_hold_followup as holds
import closer_stage_feedback as metrics


def _wait_actual_exit(pid: int, *, seconds: float = 15) -> None:
    """Do not infer process exit from the earlier result-file write."""
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        try:
            reaped, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
        else:
            if reaped == pid:
                return
        time.sleep(0.025)
    pytest.fail("actual closed-issue native worker did not exit after its result")


def test_actual_closed_complete_authenticated_receipt_counts_one_zero_model_branch(
    completed_native_world,
):
    world = completed_native_world
    obj, result = _completed_worker(world)
    worker = obj.read()["workers"][-1]
    _wait_actual_exit(worker["pid"])
    doc = campaign.reconcile(obj)
    assert doc["workers"][-1]["state"] == "terminal"
    detail = json.loads(result["detail"])
    proof = detail["completed_round"]
    receipt_path = Path(proof["receipt_path"])
    raw = receipt_path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == proof["receipt_sha256"]

    receipts = holds.validated_receipts(obj, doc, world.manifest)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["round_id"] == proof["round_id"]
    assert receipt["run_ids"] == proof["run_ids"]
    row = receipt["targets"][TARGET]
    snapshot = row["results"]["prepare"]["snapshot"]
    assert snapshot["kind"] == "issue" and snapshot["state"] == "CLOSED"
    assert snapshot.get("head") is None  # A closed issue has no invented PR head.
    assert row["results"]["interpret"]["classification"] == "deterministic_complete"
    assert row["results"]["act"]["status"] == "completed"
    assert not (world.root.parent / "provider-calls").exists()
    bindings = {
        TARGET: {
            "revision": row["revision"],
            "head": None,
            "run_id": receipt["run_ids"][TARGET],
        }
    }
    now = time.time()
    summary = metrics.summarize_efficiency(receipts, now=now, current_bindings=bindings)
    assert summary["freshness"] == "current_bindings_checked"
    assert summary["observed_branches"] == {
        "denominator": 1,
        "deterministic_zero_reasoning": 1,
        "with_retained_model_runs": 0,
        "excluded_waits": 0,
    }
    assert summary["model_runs"]["count"] == 0 and summary["model_runs"]["ids"] == []
    assert summary["observed_cost_usd"]["total_or_null"] is None
    assert summary["independent_outcomes"]["false_acceptance_count"] is None
    assert summary["independent_outcomes"]["rework_count"] is None
    assert summary["counterfactual_savings_usd"] is None
    historical = metrics.summarize_efficiency(receipts, now=now)
    assert historical["freshness"] == "historical_freshness_unverified"
    duplicate = metrics.summarize_efficiency(
        receipts + receipts, now=now, current_bindings=bindings
    )
    assert duplicate["observed_branches"] == summary["observed_branches"]
    stale = metrics.summarize_efficiency(receipts, now=now + 86401, current_bindings=bindings)
    assert stale["observed_branches"]["denominator"] == 0
    assert stale["rejected"]["stale_receipt"] == 1

    # A model run added only to unauthenticated copied bytes must not count.
    changed = json.loads(raw)
    changed["targets"][TARGET]["results"]["interpret"]["actual"] = {
        "run_id": "not-an-observed-model-submission"
    }
    try:
        receipt_path.write_text(json.dumps(changed))
        rejected = holds.validated_receipts(obj, obj.read(), world.manifest)
        assert rejected == []
        refused = metrics.summarize_efficiency(rejected, now=now)
        assert refused["observed_branches"]["denominator"] == 0
        assert refused["model_runs"]["count"] == 0
    finally:
        receipt_path.write_bytes(raw)
    assert holds.validated_receipts(obj, obj.read(), world.manifest) == receipts
    assert not (world.root.parent / "provider-calls").exists()

    destination = os.environ.get("CATEGORY46_READINESS_EVIDENCE_ROOT")
    if destination:
        out = Path(destination)
        out.mkdir(parents=True, exist_ok=True)
        (out / "closed-complete-authenticated-receipt.json").write_bytes(raw)
        (out / "closed-complete-native-proof.json").write_text(json.dumps(proof, indent=2) + "\n")
        (out / "closed-complete-summary.json").write_text(
            json.dumps(
                {
                    "schema": "category46-closed-complete-metrics-composition/v1",
                    "source_head": "3d949f9a1bfd6f3ae1d67c32cc06b8cbffff415e",
                    "evidence_class": "SYNTHETIC_ACTUAL_LOCAL_NATIVE_COMPOSITION",
                    "worker_id": worker["worker_id"],
                    "actual_pid_exit_observed": True,
                    "round_id": receipt["round_id"],
                    "run_ids": receipt["run_ids"],
                    "receipt_sha256": hashlib.sha256(raw).hexdigest(),
                    "target": TARGET,
                    "head": None,
                    "metrics": summary,
                    "historical_freshness": historical["freshness"],
                    "duplicate_branch_count": duplicate["observed_branches"]["denominator"],
                    "tampered_authentication_rejected": True,
                    "stale_receipt_rejected": True,
                    "provider_calls": 0,
                    "live_baseline": False,
                    "savings": None,
                },
                indent=2,
            )
            + "\n"
        )
