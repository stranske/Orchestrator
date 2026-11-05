"""Generous controller payloads survive every stage; anomalous input stays held."""

import time

import pytest

import closer_pipeline as pipeline
from closer_frontier_policy import action_identity, make_frontier

TARGET = "owner/repo#1"


def run_controller(path, mode, callbacks):
    common = dict(
        **callbacks,
        exception=lambda *_: {"reviewed": True},
        capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
    )
    if mode == "finite":
        frontier = make_frontier(
            [TARGET],
            owner="budget-test",
            frontier_id="generous-callback-budget",
            bindings={
                TARGET: {
                    "revision": "rev-1",
                    "action_id": action_identity(TARGET, "rev-1", "merge", {}),
                }
            },
        )
        return pipeline._run_pipeline_finite(
            path,
            [TARGET],
            frontier_target=TARGET,
            finite_frontier=frontier,
            request_timeout_seconds=30,
            **common,
        )
    return pipeline.run_pipeline(path, [TARGET], deadline=time.time() + 30, **common)


@pytest.mark.parametrize("mode", ["legacy", "finite"])
@pytest.mark.parametrize("payload_bytes", [48 * 1024, 512 * 1024])
def test_legitimate_large_results_survive_following_context_and_persistence(
    tmp_path, mode, payload_bytes
):
    payload = "x" * payload_bytes
    calls = []

    def prepare(_target, _context):
        calls.append("prepare")
        return {"revision": "rev-1", "identity": TARGET, "payload": payload}

    def interpret(_target, context):
        calls.append("interpret")
        assert context["prepare"]["payload"] == payload
        return {"substantive_needs": [], "payload": payload}

    def act(_target, context):
        calls.append("act")
        assert context["prepare"]["payload"] == context["interpret"]["payload"] == payload
        return {"status": "completed", "target_complete": True, "payload": payload}

    receipt = run_controller(
        tmp_path / "controller.json",
        mode,
        dict(prepare=prepare, interpret=interpret, act=act),
    )
    assert calls == ["prepare", "interpret", "act"]
    row = receipt["targets"][TARGET]
    assert row["status"] == "completed", row.get("error")
    for stage in calls:
        assert row["results"][stage]["payload"] == payload


@pytest.mark.parametrize("mode", ["legacy", "finite"])
@pytest.mark.parametrize("anomaly", ["oversized", "nonfinite"])
def test_anomalous_result_is_rejected_before_following_stage(tmp_path, mode, anomaly):
    calls = []
    value = "x" * (2 * 1024 * 1024) if anomaly == "oversized" else float("nan")

    def forbidden(*_args):
        calls.append("unsafe-following-stage")
        raise AssertionError("invalid result reached the next stage")

    receipt = run_controller(
        tmp_path / "controller.json",
        mode,
        dict(
            prepare=lambda *_: {"revision": "rev-1", "payload": value},
            interpret=forbidden,
            act=forbidden,
        ),
    )
    assert calls == []
    row = receipt["targets"][TARGET]
    assert row["status"] in {"held", "failed"}
    assert row["results"] == {}
    assert (
        "exceeds 1048576 bytes" in row["error"]
        if anomaly == "oversized"
        else "JSON" in row["error"]
    )
