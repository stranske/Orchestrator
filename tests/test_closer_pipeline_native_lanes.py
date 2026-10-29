"""Scheduler bookkeeping with a controlled readiness oracle, not authority proof.

Actual registered native callback/packet/queue authority is exercised separately
by the public runtime composition tests. These private scheduler units control a
packet-read race at launch/completion without providers or consequential effects.
"""

import time
from concurrent.futures import CancelledError

import pytest

import closer_dispatcher as dispatcher
import closer_frontier_policy as policy
import closer_pipeline as pipeline

BAD, PEER = "owner/repo#1", "owner/repo#2"


def callbacks(interpret):
    return {
        "prepare": lambda target, ctx: {"revision": "r", "identity": target},
        "interpret": interpret,
        "act": lambda target, ctx: {"status": "completed", "target_complete": True},
        "exception": lambda target, ctx: {"reviewed": True},
    }


@pytest.mark.parametrize("outcome", ["success", "error", "cancelled"])
@pytest.mark.parametrize("started_native", [True, False])
def test_completion_releases_launched_lane_after_readiness_changes(
    tmp_path, monkeypatch, outcome, started_native
):
    ready = {BAD: started_native, PEER: started_native}
    calls = []

    def interpret(target, context):
        calls.append(target)
        if target == BAD:
            ready[BAD] = not started_native  # The retained packet changed after launch.
            if outcome == "error":
                raise OSError("retained input disappeared")
            if outcome == "cancelled":
                raise CancelledError("owned request cancelled")
        return {"substantive_needs": []}

    monkeypatch.setattr(dispatcher, "native_stage_remote_only", lambda t, *_args: ready[t])
    # This tests lane accounting, not elapsed-time policy. Durable payload
    # checks in the execution mirror may legitimately take over two seconds.
    started_at = time.time()
    result = pipeline._run_pipeline_impl(
        tmp_path / "state.json",
        [BAD, PEER],
        **callbacks(interpret),
        capacity_fn=lambda: {"io": 1, "low": 0 if started_native else 1, "exception": 0},
        deadline=started_at + 2,
        now=lambda: started_at,
        max_io_workers=1,
        max_low_workers=1,
        max_exception_workers=1,
    )
    assert result["targets"][PEER]["status"] == "completed", result["targets"]
    assert calls == [BAD, PEER]
    assert result["targets"][BAD]["status"] == ("completed" if outcome == "success" else "failed")
    assert result["metrics"]["peak_active"] == {
        "io": 1,
        "low": 0 if started_native else 1,
        "exception": 0,
    }


def test_changed_native_packet_never_buys_zero_capacity_model_slot(tmp_path, monkeypatch):
    ready = {BAD: True, PEER: False}
    calls = []

    def interpret(target, context):
        calls.append(target)
        ready[BAD] = False
        return {"substantive_needs": []}

    monkeypatch.setattr(dispatcher, "native_stage_remote_only", lambda t, *_args: ready[t])
    started_at = time.time()
    result = pipeline._run_pipeline_impl(
        tmp_path / "state.json",
        [BAD, PEER],
        **callbacks(interpret),
        capacity_fn=lambda: {"io": 2, "low": 0, "exception": 0},
        deadline=started_at + 2,
        now=lambda: started_at,
        max_io_workers=2,
        max_low_workers=1,
        max_exception_workers=1,
    )
    assert calls == [BAD], "An IO completion must not decrement LOW below zero"
    assert result["targets"][BAD]["status"] == "completed"
    assert result["targets"][PEER]["deferred_reason"] == "worker_capacity"


@pytest.mark.parametrize("finite", [False, True])
@pytest.mark.parametrize("failure", [ValueError, TypeError, OSError, RecursionError])
def test_readiness_input_failure_is_per_item_before_callback(
    tmp_path, monkeypatch, finite, failure
):
    calls = []

    def readiness(target, *_args):
        if target == BAD:
            raise failure("malformed or unavailable bound native input")
        return True

    monkeypatch.setattr(dispatcher, "native_stage_remote_only", readiness)
    cb = callbacks(lambda target, ctx: calls.append(target) or {"substantive_needs": []})
    options = {
        "capacity_fn": lambda: {"io": 1, "low": 0, "exception": 0},
        "max_io_workers": 1,
        "max_low_workers": 1,
        "max_exception_workers": 1,
    }
    if finite:
        # Mechanics only: production public entry still authenticates callbacks.
        monkeypatch.setattr(pipeline, "_require_dispatcher_finite_callbacks", lambda _: None)
        frontier = policy.make_frontier(
            [BAD, PEER],
            owner="owned-test",
            frontier_id="native-readiness-errors",
            bindings={
                t: {"revision": "r", "action_id": policy.action_identity(t, "r", "merge", {})}
                for t in [BAD, PEER]
            },
        )
        result = pipeline._run_pipeline_finite(
            tmp_path / "state.json",
            [BAD, PEER],
            **cb,
            **options,
            finite_frontier=frontier,
            request_timeout_seconds=1,
        )
    else:
        result = pipeline._run_pipeline_impl(
            tmp_path / "state.json",
            [BAD, PEER],
            **cb,
            **options,
            deadline=time.time() + 2,
        )
    assert calls == [PEER]
    assert result["targets"][PEER]["status"] == "completed"
    row = result["targets"][BAD]
    assert row["status"] == ("held" if finite else "failed")
    assert row["error"] == "native_readiness_unavailable:" + failure.__name__
    assert row["stage"] == "interpret"
