"""Receipt-less ACT admission preserves unknown effects, not a not-started fiction."""

from __future__ import annotations

import copy
import time

import pytest

import closer_pipeline as pipeline

TARGET = "owner/repo#31"
ORIGIN = "a" * 64


def original(status="running", stage="act"):
    now = time.time()
    row = {
        "status": status,
        "stage": stage,
        "revision": "rev",
        "identity": "identity",
        "work_key": "work",
        "metrics": {"stage_seconds": {}, "errors": 0, "waits": 0},
        "results": {"prepare": {"revision": "rev"}},
        "deferred_reason": "deadline_exhausted",
        "resource_evidence": {"deadline": now - 200, "source_target": "owner/foreign#9"},
    }
    previous = {
        "created": now - 100,
        "deadline": now - 50,
        "assignment": [TARGET],
        "targets": {TARGET: row},
    }
    prior = {
        "valid": False,
        "stop_valid": False,
        "reasons": [f"actionable_pipeline_work:{TARGET}:{stage}"],
    }
    return previous, prior, now


@pytest.mark.parametrize("status", ["queued", "running"])
@pytest.mark.parametrize("stage", ["act", "reconcile"])
def test_interrupted_actions_are_audited_only_for_reconciliation(tmp_path, status, stage):
    previous, prior, now = original(status, stage)
    before = copy.deepcopy(previous)
    result = pipeline._expired_invalid_round_recovery(
        prior, previous, tmp_path / "state.json", ORIGIN, now
    )
    assert result[0]
    assert result[1:5] == (set(), set(), set(), {TARGET})
    audit = result[5]
    row = audit["targets"][TARGET]
    assert row["recovery"] == "reconcile_only"
    assert row["status"] == status and row["stage"] == stage
    assert row["item_sha256"] == pipeline._digest(before["targets"][TARGET])
    assert audit["prior_stop"] == prior and audit["source_receipt_missing"]
    assert previous == before


@pytest.mark.parametrize(
    "case",
    [
        "provider",
        "barrier",
        "completed",
        "revision",
        "identity",
        "work_key",
        "terminal_receipt",
        "unknown_reservations",
        "unrecognized_reason",
    ],
)
def test_missing_or_contradictory_authority_remains_held(tmp_path, case):
    previous, prior, now = original()
    row = previous["targets"][TARGET]
    if case == "provider":
        row["provider_outcome_uncertain"] = {"run_id": "unknown"}
    elif case == "barrier":
        row["recovery_barrier"] = {}
    elif case == "completed":
        row["known_completed_action"] = True
    elif case in {"revision", "identity", "work_key"}:
        row.pop(case)
    elif case == "terminal_receipt":
        previous["receipt_summary"] = {"finished_at": now - 40}
    elif case == "unknown_reservations":
        prior["reasons"].append("unresolved_resource_reservations")
    else:
        prior["reasons"].append("unrecognized_stop_defect")
    before = copy.deepcopy(previous)
    assert not pipeline._expired_invalid_round_recovery(
        prior, previous, tmp_path / "state.json", ORIGIN, now
    )[0]
    assert previous == before


@pytest.mark.parametrize("status", ["queued", "running"])
def test_real_restart_preserves_original_row_and_never_reexecutes_act(
    tmp_path, monkeypatch, status
):
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "private-state"))
    path = tmp_path / "state.json"
    previous, _prior, _now = original(status)
    raw_row = copy.deepcopy(previous["targets"][TARGET])
    state = pipeline._empty()
    state["runs"][ORIGIN] = previous
    pipeline._save(path, state)
    calls = []

    def forbidden(*_):
        pytest.fail("an interrupted action cannot prepare, dispatch a worker or execute ACT")

    def reconcile(target, context):
        calls.append((target, copy.deepcopy(context)))
        return {"outcome": "unknown", "reason": "independent original outcome proof unavailable"}

    pipeline.run_pipeline(
        path,
        [TARGET],
        prepare=forbidden,
        interpret=forbidden,
        exception=forbidden,
        act=forbidden,
        reconcile=reconcile,
        capacity_fn=lambda: {"io": 1, "low": 0, "exception": 0},
        deadline=time.time() + 3,
    )
    assert len(calls) == 1 and calls[0][0] == TARGET
    current = max(pipeline._load(path)["runs"].values(), key=lambda row: row["created"])
    audit = current["invalid_stop_history"][-1]
    assert audit["source_run_id"] == ORIGIN
    assert audit["targets"][TARGET]["status"] == status
    assert audit["targets"][TARGET]["stage"] == "act"
    assert audit["targets"][TARGET]["item_sha256"] == pipeline._digest(raw_row)
    assert current["targets"][TARGET]["status"] == "uncertain"
    assert current["targets"][TARGET]["stage"] == "reconcile"
    assert not any(result.get("act") for result in [current["targets"][TARGET]["results"]])
