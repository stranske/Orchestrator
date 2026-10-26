"""Independent offline accounting against real authenticated descendant receipts."""

import copy
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from test_closer_ci_followup import public_ci_world as public_ci_world
from test_closer_hold_descendant_boundary import captured
from test_closer_native_capacity import capacity_host as capacity_host
from test_closer_native_descendants import native_descendant_world as native_descendant_world
from test_closer_native_observer import host as host

import closer_hold_followup as holds
from closer_stage_feedback import summarize_efficiency


def test_offline_measurement_cli_preserves_unknowns_and_labels_fixture_values(tmp_path):
    workload = Path("tests/fixtures/closer-category46/representative-workload.json")
    before = workload.read_bytes()
    output = tmp_path / "measurement.json"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/measure_closer_acceptance.py",
            str(workload),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text())
    assert report["evidence_kind"] == "synthetic_fixture"
    summary = report["summary"]
    assert summary["observed_branches"] == {
        "denominator": 5,
        "deterministic_zero_reasoning": 2,
        "with_retained_model_runs": 3,
        "excluded_waits": 4,
    }
    assert summary["model_runs"]["count"] == 3
    assert summary["observed_cost_usd"]["coverage"] == 2 / 3
    assert summary["observed_cost_usd"]["status"] == "partial"
    assert summary["counterfactual_savings_usd"] is None
    assert report["baseline"]["live_model_call_avoidance"] is None
    assert report["baseline"]["live_savings_usd"] is None
    assert report["measurement_plumbing"]["wall_seconds"] >= 0
    assert workload.read_bytes() == before


def test_actual_completed_post_is_work_while_downstream_verifier_stays_unknown(
    native_descendant_world,
):
    obj, doc, _row, manifest = captured(native_descendant_world)
    receipts = holds.validated_receipts(obj, doc, manifest)
    assert len(receipts) == 3
    result = summarize_efficiency(receipts, holds=doc["hold_followups"].values(), now=time.time())
    assert result["observed_branches"]["denominator"] == 2
    assert result["observed_branches"]["deterministic_zero_reasoning"] == 2
    assert result["model_runs"]["count"] == 0
    assert result["independent_outcomes"]["false_acceptance_rate"] is None
    assert result["counterfactual_savings_usd"] is None
    assert result["observed_branches"]["excluded_waits"] == 1
    original = copy.deepcopy(receipts)
    post_index = next(
        i
        for i, receipt in enumerate(receipts)
        if any(
            row.get("deferred_reason") == "accepted_verifier_request"
            for row in receipt["targets"].values()
        )
    )
    post_row = next(iter(receipts[post_index]["targets"].values()))
    completed_post = post_row["results"]["act"]["queue_receipt"]["accounting"][0]
    # The real producer now persists the completed POST before observing its
    # result-wait. This is not a fixture invented to look completed.
    assert completed_post["operation_status"] == "completed"
    assert completed_post["eligible"] is False
    assert completed_post["reason"] == (
        "collect terminal or unattributed verifier evidence; do not rerun"
    )

    def rebind_queue(row):
        queue = row["results"]["act"]["queue_receipt"]
        row["queue_receipt"] = copy.deepcopy(queue)
        row["resource_evidence"]["accepted_verifier_request"]["queue_receipt_sha256"] = (
            hashlib.sha256(
                json.dumps(queue, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )

    # Read-side join compatibility only: retain the older exact progress shape
    # and the other established accepted-result reason, without changing proof.
    for eligible, reason in (
        (True, "pass_limit"),
        (False, "same verification request already accepted"),
    ):
        compatible = copy.deepcopy(receipts)
        row = next(iter(compatible[post_index]["targets"].values()))
        entry = row["results"]["act"]["queue_receipt"]["accounting"][0]
        entry.update(eligible=eligible, reason=reason)
        rebind_queue(row)
        observed = summarize_efficiency(compatible, now=time.time())
        assert observed["observed_branches"]["denominator"] == 2
        assert observed["model_runs"]["count"] == 0

    # One real producer fixture, then independent adversarial read-side joins.
    for damage in (
        "proof",
        "head",
        "operation",
        "request",
        "queue_hash",
        "events",
        "completion",
        "status",
        "row_queue",
        "event_id",
        "frontier",
        "merge",
        "accounting",
        "duplicate",
        "frontier_missing_policy",
        "frontier_schema",
        "frontier_owner",
        "frontier_target_digest",
        "frontier_binding_digest",
        "frontier_binding_rehashed",
        "frontier_revision_rehashed",
        "generic_deferred",
        "generic_pending",
        "pending_status",
        "wrong_eligibility",
        "wrong_action",
        "wrong_submitted_operation",
    ):
        bad = copy.deepcopy(receipts)
        row = next(iter(bad[post_index]["targets"].values()))
        proof = row["resource_evidence"]["accepted_verifier_request"]
        act = row["results"]["act"]
        queue = act["queue_receipt"]
        if damage.startswith("frontier_"):
            frontier = bad[post_index]["finite_frontier"]
            target = row["results"]["prepare"]["snapshot"]["target"]
            if damage == "frontier_missing_policy":
                bad[post_index]["finite_frontier"] = {
                    "frontier_id": frontier["frontier_id"],
                    "targets": [target],
                }
            elif damage == "frontier_schema":
                frontier["schema"] = "unknown-policy"
            elif damage == "frontier_owner":
                frontier.pop("owner")
            elif damage == "frontier_target_digest":
                frontier["targets_sha256"] = "0" * 64
            elif damage == "frontier_binding_digest":
                frontier["bindings"][target]["action_id"] = "0" * 64
            else:
                if damage == "frontier_binding_rehashed":
                    frontier["bindings"][target]["action_id"] = "invalid-action-id"
                else:
                    frontier["bindings"][target]["revision"] = "unrelated-revision"
                frontier["bindings_sha256"] = hashlib.sha256(
                    json.dumps(frontier["bindings"], sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
        elif damage == "proof":
            row.pop("resource_evidence")
        elif damage == "head":
            proof["head"] = "b" * 40
        elif damage == "frontier":
            proof["operation_frontier"] = "unrelated-frontier"
        elif damage == "merge":
            act["pending_evidence"]["merge"] = "b" * 40
        elif damage == "operation":
            proof["operation_id"] = "unrelated"
        elif damage == "request":
            act["pending_evidence"]["event_id"] = "b" * 64
        elif damage == "queue_hash":
            proof["queue_receipt_sha256"] = "0" * 64
        elif damage == "status":
            row["status"] = "unknown"
        elif damage == "row_queue":
            row["queue_receipt"] = {}
        elif damage == "event_id":
            proof["event_id"] = act["pending_evidence"]["event_id"] = ""
        else:
            if damage == "events":
                queue["events"] = []
            elif damage == "accounting":
                queue["accounting"][0]["reason"] = "unknown_result"
            elif damage == "duplicate":
                queue["accounting"].append(copy.deepcopy(queue["accounting"][0]))
            elif damage in {"generic_deferred", "generic_pending"}:
                queue["accounting"][0]["reason"] = damage.removeprefix("generic_")
            elif damage == "pending_status":
                queue["accounting"][0]["status"] = "pending"
            elif damage == "wrong_eligibility":
                queue["accounting"][0]["eligible"] = True
            elif damage == "wrong_action":
                queue["accounting"][0]["action"] = "wait"
            elif damage == "wrong_submitted_operation":
                for event in queue["events"]:
                    if event.get("kind") == "operation_submitted":
                        event["operation_id"] = "unrelated-original-operation"
            else:
                queue["accounting"][0]["operation_status"] = "submitted"
            rebind_queue(row)
        rejected = summarize_efficiency(bad, now=time.time())
        assert rejected["observed_branches"]["denominator"] == 1, damage
        assert rejected["model_runs"]["count"] == 0, damage
    assert receipts == original
    artifact_dir = os.environ.get("CLOSER_ACCEPTANCE_ARTIFACT_DIR")
    if artifact_dir:
        path = Path(artifact_dir)
        path.mkdir(parents=True, exist_ok=True)
        (path / "authenticated-descendant-summary.json").write_text(
            json.dumps(
                {
                    "evidence_kind": "synthetic_external_boundaries_actual_authenticated_local_producers",
                    "provider_calls": 0,
                    "deployment": False,
                    "summary": result,
                    "negative_join_cases": 27,
                    "producer_accounting": completed_post,
                    "supported_join_shapes": [
                        "original eligible pass_limit progress",
                        "ineligible exact accepted-request reason",
                        "ineligible exact collect-terminal do-not-rerun reason",
                    ],
                    "source_hashes": {
                        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in (Path("src/closer_stage_feedback.py"), Path(__file__))
                    },
                    "scope": "CI wait excluded; completed merge and POST counted; verifier outcome unmeasured",
                },
                indent=2,
            )
            + "\n"
        )
