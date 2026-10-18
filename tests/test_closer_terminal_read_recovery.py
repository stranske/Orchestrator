"""Offline native-wait provenance across failed prelaunch read cadences."""

from __future__ import annotations

import copy
import hashlib

import pytest
from test_closer_pipeline import callbacks, run
from test_closer_retained_read_deferral import recovered_wait

import closer_pipeline as pipeline
import closer_queue


def busy_read_failure(path):
    at, target, inner, original = recovered_wait(path)
    clock = [at + 20]
    prepared = []

    def prepare(name, _context):
        prepared.append(name)
        old = original["targets"][target]
        return {
            "revision": old["revision"],
            "identity": old["identity"],
            "snapshot": copy.deepcopy(old["results"]["prepare"]["snapshot"]),
        }

    def interpret(_name, context):
        clock[0] = context["deadline"] + 1
        # The real route selector raises this before calling a provider; do not
        # substitute a provider failure/success receipt for a prelaunch timeout.
        raise RuntimeError("low provider route remained internally busy through deadline")

    cb = {
        **callbacks(interpret=interpret, act=lambda *_: pytest.fail("failed read cannot act")),
        "prepare": prepare,
    }
    result = run(path, [target], cb, seconds=10, now=lambda: clock[0])
    current = next(iter(pipeline._load(path)["runs"].values()))
    assert result.get("status") != "blocked"
    assert current["targets"][target]["status"] == "failed"
    assert current["targets"][target]["error"] == pipeline.PRELAUNCH_ROUTE_BUSY_ERROR
    return at, target, inner, original, current, clock, prepared, cb


def test_failed_prelaunch_read_retries_freshly_with_exact_original_wait_audit(tmp_path):
    path = tmp_path / "pipeline.json"
    at, target, inner, original, current, clock, prepared, cb = busy_read_failure(path)
    original_act = original["targets"][target]["results"]["act"]
    original_audit = copy.deepcopy(original["invalid_stop_history"])
    original_anchor = original["recovered_invalid_stop"]["targets"][target]
    native_sha = hashlib.sha256(inner.read_bytes()).hexdigest()
    quarantines = {
        name: row["recovery_barrier"]
        for name, row in original["targets"].items()
        if row.get("recovery_barrier")
    }
    uncertain = {
        name for name, row in original["targets"].items() if row.get("status") == "uncertain"
    }
    for cadence in range(3):
        row = current["targets"][target]
        assert row["status"] == "failed" and row["stage"] == "interpret"
        assert row["results"]["act"] == original_act
        assert row["error"] == pipeline.PRELAUNCH_ROUTE_BUSY_ERROR
        stop = pipeline.validate_stop(path, current["assignment"], now=lambda: clock[0])
        assert f"unresolved_pipeline_work:{target}:failed" in stop["reasons"]
        recovery = pipeline._expired_invalid_round_recovery(
            stop, current, path, current["receipt_summary"]["run_id"], clock[0] + 20
        )
        assert recovery[0] and target in recovery[1]
        assert recovery[3] == set(quarantines) and recovery[4] == uncertain
        audited_failure = recovery[-1]["targets"][target]
        assert audited_failure["status"] == "failed"
        assert audited_failure["error"] == pipeline.PRELAUNCH_ROUTE_BUSY_ERROR
        assert audited_failure["act_result"] == original_act
        assert audited_failure["recovery"] == "retry_fresh"
        assert current["invalid_stop_history"][: len(original_audit)] == original_audit
        anchor = current["recovered_invalid_stop"]["targets"][target]
        assert all(anchor[key] == value for key, value in original_anchor.items())
        assert hashlib.sha256(inner.read_bytes()).hexdigest() == native_sha
        if cadence < 2:
            clock[0] += 20
            result = run(path, [target], cb, seconds=10, now=lambda: clock[0])
            assert result.get("status") != "blocked"
            current = next(iter(pipeline._load(path)["runs"].values()))
    assert prepared == [target] * 3


@pytest.mark.parametrize(
    "mutation",
    [
        "different_error",
        "unbound_provider_error",
        "stage_prepare",
        "stage_act",
        "early_finish",
        "finished_missing",
        "finished_null",
        "finished_string",
        "finished_bool",
        "finished_nan",
        "finished_infinite",
        "finished_future",
        "finished_before_created",
        "receipt_missing",
        "receipt_null",
        "receipt_run",
        "receipt_schema",
        "receipt_deadline",
        "known_completed",
        "provider_uncertainty",
        "head",
        "audit",
        "marker",
        "source",
        "native_operation",
    ],
)
def test_failed_read_retained_wait_never_hides_unknown_or_mutating_outcome(tmp_path, mutation):
    path = tmp_path / "pipeline.json"
    _at, target, inner, _original, _current, clock, _prepared, _cb = busy_read_failure(path)
    state = pipeline._load(path)
    current = next(iter(state["runs"].values()))
    current_run_id = current["receipt_summary"]["run_id"]
    row = current["targets"][target]
    if mutation == "different_error":
        row["error"] = "RuntimeError: unclassified callback failure"
    elif mutation == "unbound_provider_error":
        row["error"] = pipeline.UNBOUND_UNCERTAIN_PROVIDER_ERROR
    elif mutation.startswith("stage_"):
        row["stage"] = mutation[6:]
    elif mutation == "early_finish":
        current["receipt_summary"]["finished_at"] = current["deadline"] - 1
    elif mutation.startswith("finished_"):
        if mutation == "finished_missing":
            current["receipt_summary"].pop("finished_at")
        else:
            current["receipt_summary"]["finished_at"] = {
                "finished_null": None,
                "finished_string": str(clock[0]),
                "finished_bool": True,
                "finished_nan": float("nan"),
                "finished_infinite": float("inf"),
                "finished_future": clock[0] + 1000,
                "finished_before_created": current["created"] - 1,
            }[mutation]
    elif mutation == "receipt_missing":
        current.pop("receipt_summary")
    elif mutation == "receipt_null":
        current["receipt_summary"] = None
    elif mutation.startswith("receipt_"):
        key = {"receipt_run": "run_id", "receipt_schema": "schema", "receipt_deadline": "deadline"}[
            mutation
        ]
        current["receipt_summary"][key] = "different"
    elif mutation == "known_completed":
        row["known_completed_action"] = True
    elif mutation == "provider_uncertainty":
        row["provider_outcome_uncertain"] = {"unresolved": True}
    elif mutation == "head":
        row["results"]["prepare"]["snapshot"]["head"] = "c" * 40
    elif mutation == "audit":
        for entry in current["invalid_stop_history"]:
            if (
                target in entry["targets"]
                and entry["targets"][target].get("recovery") == "validated_inner_wait"
            ):
                entry["targets"][target]["act_result"] = {
                    "status": "completed",
                    "target_complete": True,
                }
    elif mutation == "marker":
        current["prior_invalid_stop_evidence"]["recovered_inner_wait_targets"].remove(target)
    elif mutation == "source":
        current["recovered_invalid_stop"]["targets"][target]["previous_run_id"] = "unrelated"
    else:
        native = closer_queue._read_state(inner)
        native["items"][target]["operation"] = {"status": "uncertain", "operation_id": "unknown"}
        inner.write_text(__import__("json").dumps(native))
    if mutation not in {"finished_nan", "finished_infinite"}:
        pipeline._save(path, state)
    stop = pipeline.validate_stop(path, current["assignment"], now=lambda: clock[0])
    recovery = pipeline._expired_invalid_round_recovery(
        stop, current, path, current_run_id, clock[0] + 20
    )
    assert target not in recovery[1]
    if mutation == "stage_act":
        assert recovery[0] and target in recovery[3]
    else:
        assert not recovery[0]
