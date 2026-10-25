from __future__ import annotations

import fcntl
import multiprocessing
import time

import pytest

import closer_pipeline as cp
import closer_queue
from closer_frontier_policy import action_identity, make_frontier


def _hold_pipeline_lock(path, ready, release=None):
    lock_path = str(path) + ".lock"
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        ready.set()
        if release is None:
            time.sleep(0.5)
        else:
            release.wait(timeout=5)


def _frontier_with_timeout(target, timeout=0.1):
    return (
        make_frontier(
            [target],
            owner="frontier-owner",
            frontier_id="frontier-pipeline-lock-test",
            bindings={
                target: {
                    "revision": "rev-1",
                    "action_id": action_identity(
                        target, "rev-1", "merge", {"pr_target": target, "source_targets": []}
                    ),
                }
            },
        ),
        timeout,
    )


def _frontier(target):
    return make_frontier(
        [target],
        owner="frontier-owner",
        frontier_id="frontier-pipeline-test",
        bindings={
            target: {
                "revision": "rev-1",
                "action_id": action_identity(
                    target, "rev-1", "merge", {"pr_target": target, "source_targets": []}
                ),
            }
        },
    )


def _run(path, target, frontier, *, now, **callbacks):
    return cp._run_pipeline_finite(
        path,
        [target],
        frontier_target=target,
        finite_frontier=frontier,
        request_timeout_seconds=10,
        capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
        prepare=callbacks.get("prepare", lambda _target, _ctx: {"revision": "rev-1"}),
        interpret=callbacks.get("interpret", lambda _target, _ctx: {"substantive_needs": []}),
        exception=callbacks.get("exception", lambda _target, _ctx: {"route": "normal"}),
        act=callbacks.get(
            "act", lambda _target, _ctx: {"status": "completed", "target_complete": True}
        ),
        now=now,
    )


def test_finite_pipeline_has_no_aggregate_elapsed_cutoff(tmp_path):
    target = "acme/repo#1"
    frontier = _frontier(target)
    clock = [1_000.0]
    windows = []

    def callback(result):
        def call(_target, context):
            windows.append((context["request_started_at"], context["deadline"]))
            assert context["deadline"] - context["request_started_at"] == 10
            assert context["finite_frontier"] == frontier
            clock[0] += 9
            return result

        return call

    receipt = _run(
        tmp_path / "pipeline.json",
        target,
        frontier,
        now=lambda: clock[0],
        prepare=callback({"revision": "rev-1"}),
        interpret=callback({"substantive_needs": []}),
        act=callback({"status": "completed", "target_complete": True}),
    )

    assert clock[0] - 1_000 > 20  # the legacy aggregate window used by this fixture
    assert len(windows) == 3
    assert windows[1][0] - windows[0][0] == 9
    assert receipt["status"] == "completed"
    assert receipt["valid"] and receipt["stop_valid"]
    assert "deadline" not in receipt
    checked = cp.validate_stop(
        tmp_path / "pipeline.json",
        [target],
        finite_frontier=frontier,
        frontier_target=target,
        expected_run_id=receipt["run_id"],
    )
    assert checked["valid"] and checked["targets"][target]["status"] == "completed"
    assert "deadline" not in checked


def test_finite_pipeline_never_replays_unknown_action_after_restart(tmp_path):
    target = "acme/repo#2"
    frontier = _frontier(target)
    calls = []
    clock = [2_000.0]

    def act(*_args):
        calls.append("effect")
        raise TimeoutError("possibly submitted")

    first = _run(tmp_path / "pipeline.json", target, frontier, now=lambda: clock[0], act=act)
    second = _run(tmp_path / "pipeline.json", target, frontier, now=lambda: clock[0], act=act)

    assert calls == ["effect"]
    assert first["targets"][target]["status"] == "unknown"
    assert second["targets"][target]["status"] == "unknown"
    assert not second["valid"] and not second["stop_valid"]
    assert (
        cp.validate_stop(
            tmp_path / "pipeline.json",
            [target],
            finite_frontier=frontier,
            frontier_target=target,
        )["valid"]
        is False
    )


def test_finite_pipeline_capacity_and_cancel_gates_stop_new_callbacks(tmp_path):
    target = "acme/repo#3"
    frontier = _frontier(target)
    calls = []
    receipt = cp._run_pipeline_finite(
        tmp_path / "capacity.json",
        [target],
        frontier_target=target,
        finite_frontier=frontier,
        request_timeout_seconds=10,
        capacity_fn=lambda: {"io": 1, "low": 0, "exception": 1},
        prepare=lambda *_: calls.append("prepare") or {"revision": "rev-1"},
        interpret=lambda *_: calls.append("interpret") or {"substantive_needs": []},
        exception=lambda *_: {},
        act=lambda *_: {},
        now=lambda: 3_000.0,
    )
    assert calls == ["prepare"]
    assert receipt["targets"][target]["status"] == "capacity_blocked"

    cancel_calls = []
    cancelled = cp._run_pipeline_finite(
        tmp_path / "cancelled.json",
        [target],
        frontier_target=target,
        finite_frontier=frontier,
        request_timeout_seconds=10,
        capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
        prepare=lambda *_: cancel_calls.append("prepare") or {"revision": "rev-1"},
        interpret=lambda *_: {},
        exception=lambda *_: {},
        act=lambda *_: {},
        cancelled_fn=lambda: True,
        now=lambda: 3_000.0,
    )
    assert cancel_calls == []
    assert cancelled["targets"][target]["status"] == "cancelled"


def test_finite_pipeline_rejects_revision_drift_before_interpret(tmp_path):
    target = "acme/repo#4"
    frontier = _frontier(target)
    calls = []
    receipt = _run(
        tmp_path / "revision.json",
        target,
        frontier,
        now=lambda: 4_000.0,
        prepare=lambda *_: {"revision": "different"},
        interpret=lambda *_: calls.append("interpret") or {"substantive_needs": []},
    )
    assert calls == []
    assert receipt["targets"][target]["status"] == "held"


def test_finite_inner_wait_uses_its_frontier_receipt(tmp_path):
    target = "acme/repo#inner-wait"
    revision = "rev-wait"
    head = "a" * 40
    trigger = cp.EVIDENCE_REFUSAL_TRIGGER
    binding = {"pr_target": target, "source_targets": []}
    frontier = make_frontier(
        [target],
        owner="frontier-owner",
        frontier_id="frontier-inner-wait-test",
        bindings={
            target: {
                "revision": revision,
                "action_id": action_identity(target, revision, "wait", binding),
            }
        },
    )
    pipeline_path = tmp_path / "pipeline.json"
    inner_path = (
        pipeline_path.parent / f"{pipeline_path.stem}.actions" / f"{cp._digest(target)}.json"
    )
    proof_box = {}
    receipt_box = {}

    def act(_target, context):
        closer_queue.run_round(
            inner_path,
            [target],
            lambda _item: {
                "target": target,
                "revision": revision,
                "action": "wait",
                "eligible": False,
                "requires_coordinator": False,
                "reason": "worker_json_invalid",
                "next_trigger": trigger,
                "evidence": {"head": "c" * 40},
                "operation_binding": binding,
            },
            lambda *_args: pytest.fail("legacy inner wait must not execute"),
            deadline=time.time() + 10,
            max_workers=1,
            max_passes=1,
        )
        receipt = closer_queue.run_round(
            inner_path,
            [target],
            lambda _item: {
                "target": target,
                "revision": revision,
                "action": "wait",
                "eligible": False,
                "requires_coordinator": False,
                "reason": "worker_json_invalid",
                "next_trigger": trigger,
                "evidence": {"head": head},
                "operation_binding": binding,
            },
            lambda *_args: pytest.fail("finite inner wait must not execute"),
            deadline=None,
            receipt_reserve_seconds=0,
            max_workers=1,
            max_passes=1,
            max_operation_retries=0,
            finite_frontier=frontier,
            frontier_target=target,
            request_timeout_seconds=10,
        )
        assert receipt["valid"] and receipt["stop_valid"]
        proof = {
            "schema": cp.INNER_WAIT_SCHEMA,
            "target": target,
            "revision": revision,
            "head": head,
            "reason": "worker_json_invalid",
            "next_trigger": trigger,
            "inner_path": str(inner_path),
            "receipt_sha256": cp._digest(receipt),
            "frontier_id": frontier["frontier_id"],
            "finite_frontier": frontier,
        }
        proof_box["proof"] = proof
        receipt_box["receipt"] = receipt
        return {
            "status": "deferred",
            "reason": "validated_inner_wait",
            "next_trigger": trigger,
            "resource_evidence": {"inner_wait": proof},
            "queue_receipt": receipt,
        }

    outer = _run(
        pipeline_path,
        target,
        frontier,
        now=lambda: 1_000.0,
        prepare=lambda _target, _context: {
            "revision": revision,
            "snapshot": {"target": target, "revision": revision, "head": head, "kind": "pr"},
        },
        act=act,
    )
    assert outer["targets"][target]["status"] == "held"
    assert outer["valid"] and outer["stop_valid"]
    proof = proof_box["proof"]
    selected_receipt = receipt_box["receipt"]
    other_frontier = make_frontier(
        [target],
        owner="other-frontier-owner",
        frontier_id="other-inner-wait-test",
        bindings={
            target: {
                "revision": revision,
                "action_id": action_identity(target, revision, "wait", binding),
            }
        },
    )
    other_receipt = closer_queue.run_round(
        inner_path,
        [target],
        lambda _item: {
            "target": target,
            "revision": revision,
            "action": "wait",
            "eligible": False,
            "requires_coordinator": False,
            "reason": "worker_json_invalid",
            "next_trigger": trigger,
            "evidence": {"head": "b" * 40},
            "operation_binding": binding,
        },
        lambda *_args: pytest.fail("other frontier wait must not execute"),
        deadline=None,
        receipt_reserve_seconds=0,
        max_workers=1,
        max_passes=1,
        max_operation_retries=0,
        finite_frontier=other_frontier,
        frontier_target=target,
        request_timeout_seconds=10,
    )
    state = closer_queue._read_state(inner_path)
    # The legacy bucket is a separate decoy created first. It can be budget-
    # deferred before observation, but it must never supply the finite wait's
    # receipt or item evidence.
    assert state["items"][target]["status"] == "deferred"
    assert state["items"][target]["evidence"] == {}
    assert (
        closer_queue.state_for_frontier(state, frontier["frontier_id"], expected_policy=frontier)[
            "items"
        ][target]["evidence"]["head"]
        == head
    )
    assert (
        closer_queue.state_for_frontier(
            state, other_frontier["frontier_id"], expected_policy=other_frontier
        )["items"][target]["evidence"]["head"]
        == "b" * 40
    )
    corrupt = dict(state)
    corrupt["frontiers"] = dict(state["frontiers"])
    corrupt["frontiers"][frontier["frontier_id"]] = None
    with pytest.raises(closer_queue.QueueStateError, match="bucket is corrupt"):
        closer_queue.state_for_frontier(corrupt, frontier["frontier_id"], expected_policy=frontier)
    assert closer_queue.validate_stop(
        inner_path,
        [target],
        receipt=selected_receipt,
        frontier_id=frontier["frontier_id"],
    )["stop_valid"]
    assert not closer_queue.validate_stop(
        inner_path,
        [target],
        receipt=selected_receipt,
        frontier_id=other_frontier["frontier_id"],
    )["stop_valid"]
    assert other_receipt["stop_valid"]
    assert cp._valid_inner_wait(
        pipeline_path,
        target,
        revision,
        {"target": target, "revision": revision, "head": head, "kind": "pr"},
        proof["reason"],
        trigger,
        proof,
        frontier_id=frontier["frontier_id"],
    )
    assert not cp._valid_inner_wait(
        pipeline_path,
        target,
        revision,
        {"target": target, "revision": revision, "head": head, "kind": "pr"},
        proof["reason"],
        trigger,
        proof,
        frontier_id=other_frontier["frontier_id"],
    )
    same_id_different_policy = {**frontier, "owner": "different-owner"}
    assert not cp._valid_inner_wait(
        pipeline_path,
        target,
        revision,
        {"target": target, "revision": revision, "head": head, "kind": "pr"},
        proof["reason"],
        trigger,
        proof,
        frontier_id=frontier["frontier_id"],
        finite_frontier=same_id_different_policy,
    )
    with pytest.raises(closer_queue.QueueStateError, match="missing"):
        closer_queue.state_for_frontier(state, "missing-inner-wait-test")
    assert (
        closer_queue.state_for_frontier(state, "missing-inner-wait-test", allow_missing=True)
        is None
    )
    restart = closer_queue.run_round(
        inner_path,
        [target],
        lambda *_args: pytest.fail("finished frontier must not reobserve on restart"),
        lambda *_args: pytest.fail("finished frontier must not replay on restart"),
        deadline=None,
        receipt_reserve_seconds=0,
        max_workers=1,
        max_passes=1,
        max_operation_retries=0,
        finite_frontier=frontier,
        frontier_target=target,
        request_timeout_seconds=10,
    )
    assert restart == selected_receipt
    stop = cp.validate_stop(
        pipeline_path,
        [target],
        finite_frontier=frontier,
        frontier_target=target,
    )
    assert stop["stop_valid"]
    damaged_state = closer_queue._read_state(inner_path)
    damaged_state["frontiers"] = dict(damaged_state["frontiers"])
    damaged_state["frontiers"][frontier["frontier_id"]] = None
    closer_queue._atomic_write(inner_path, damaged_state)
    damaged_stop = cp.validate_stop(
        pipeline_path,
        [target],
        finite_frontier=frontier,
        frontier_target=target,
    )
    assert not damaged_stop["stop_valid"]
    assert "finite_frontier_unverified_inner_wait" in damaged_stop["reasons"]


def test_finite_interpret_uncertainty_never_advances_to_action(tmp_path):
    target = "acme/repo#5"
    frontier = _frontier(target)
    action_calls = []
    receipt = _run(
        tmp_path / "interpret-uncertain.json",
        target,
        frontier,
        now=lambda: 5_000.0,
        interpret=lambda *_: {
            "status": "uncertain",
            "substantive_needs": [],
            "provider_outcome_uncertain": {"kind": "possibly_submitted"},
        },
        act=lambda *_: action_calls.append("act")
        or {"status": "completed", "target_complete": True},
    )
    assert action_calls == []
    assert receipt["targets"][target]["status"] == "unknown"
    assert not receipt["valid"]


def test_finite_interpret_provider_failure_holds_without_act(tmp_path):
    target = "acme/repo#provider-failure"
    frontier = _frontier(target)
    act_calls = []
    failure = {
        "provider_failure": {
            "profile_id": "claude-low",
            "agent": "claude",
            "run_id": "run-1",
            "category": "transport_error",
            "evidence_hash": "a" * 64,
            "exit": 1,
        }
    }
    receipt = _run(
        tmp_path / "provider-failure.json",
        target,
        frontier,
        now=lambda: 6_000.0,
        interpret=lambda *_: {
            "status": "deferred",
            "reason": "provider_failure",
            "next_trigger": "provider repaired",
            "resource_evidence": failure,
            "substantive_needs": [],
        },
        act=lambda *_: act_calls.append("act") or {"status": "completed", "target_complete": True},
    )
    row = receipt["targets"][target]
    assert act_calls == []
    assert row["status"] == "held"
    assert row["deferred_reason"] == "provider_failure"
    assert row["deferred_next_trigger"] == "provider repaired"
    assert receipt["valid"] and receipt["stop_valid"]


@pytest.mark.parametrize(
    "result",
    [
        {"status": "pending", "pending_kind": "ci", "next_trigger": "check terminal"},
        {"status": "deferred", "reason": "operation_running", "next_trigger": "reconcile"},
        {"status": "deferred", "reason": "made_up_wait", "next_trigger": "later"},
    ],
)
def test_finite_act_wait_without_exact_queue_proof_stays_unknown(tmp_path, result):
    target = "acme/repo#unproven-wait"
    frontier = _frontier(target)
    receipt = _run(
        tmp_path / f"unproven-{result['status']}-{result.get('reason', 'pending')}.json",
        target,
        frontier,
        now=lambda: 7_000.0,
        act=lambda *_: result,
    )
    assert receipt["targets"][target]["status"] == "unknown"
    assert not receipt["valid"] and not receipt["stop_valid"]


def test_finite_act_pending_requires_and_accepts_exact_queue_proof(tmp_path):
    target = "acme/repo#verified-pending"
    frontier = make_frontier(
        [target],
        owner="frontier-owner",
        frontier_id="frontier-verified-pending",
        bindings={
            target: {
                "revision": "rev-1",
                "action_id": action_identity(
                    target,
                    "rev-1",
                    "wait",
                    {"pr_target": target, "source_targets": []},
                ),
            }
        },
    )

    def act(_target, context):
        action_state = context["action_state_path"]
        queue_receipt = closer_queue.run_round(
            action_state,
            [target],
            lambda _item: {
                "revision": "rev-1",
                "action": "wait",
                "eligible": False,
                "reason": "current-head checks pending or unknown",
                "next_trigger": "check run reaches a terminal conclusion",
                "target": target,
                "operation_binding": {"pr_target": target, "source_targets": []},
                "claim_targets": [target],
                "repo_keys": ["acme/repo"],
            },
            lambda *_args, **_kwargs: pytest.fail("wait rows cannot execute"),
            owner="runner-a",
            deadline=None,
            receipt_reserve_seconds=0,
            max_operation_retries=0,
            finite_frontier=frontier,
            frontier_target=target,
            request_timeout_seconds=10,
        )
        return {
            "status": "pending",
            "pending_kind": "ci",
            "next_trigger": "check run reaches a terminal conclusion",
            "pending_evidence": {
                "check_run_id": 123,
                "target": target,
                "observed_revision": "rev-1",
            },
            "queue_receipt": queue_receipt,
        }

    receipt = _run(
        tmp_path / "verified-pending.json",
        target,
        frontier,
        now=lambda: 8_000.0,
        act=act,
    )
    row = receipt["targets"][target]
    assert row["status"] == "held"
    assert row["deferred_reason"] == "ci"
    assert row["deferred_next_trigger"] == "check run reaches a terminal conclusion"
    assert receipt["valid"] and receipt["stop_valid"]


def _accepted_request_round(tmp_path, *, change_evidence=None, pipeline_path=None, effect=None):
    target = "acme/repo#42"
    binding = {"pr_target": target, "source_targets": [], "head": "a" * 40, "merge": "b" * 40}
    frontier = make_frontier(
        [target],
        owner="owner",
        frontier_id="accepted-request",
        bindings={
            target: {
                "revision": "rev-1",
                "action_id": action_identity(target, "rev-1", "post_merge_verify", binding),
            }
        },
    )
    posts = []
    retained = {}

    def act(_target, context):
        action_state = context["action_state_path"]

        def observe(_name):
            return {
                "target": target,
                "revision": "rev-1",
                "head": binding["head"],
                "merge": binding["merge"],
                "pr_target": target,
                "action": "wait" if posts else "post_merge_verify",
                "eligible": not posts,
                "reason": (
                    "same verification request already accepted" if posts else "missing result"
                ),
                "next_trigger": "authenticated verifier result",
                "operation_binding": binding,
                "claim_targets": [target],
                "repo_keys": ["acme/repo"],
                "evidence": {
                    "target": target,
                    "pr_target": target,
                    "head": binding["head"],
                    "merge": binding["merge"],
                },
            }

        def execute(*_args, **_kwargs):
            posts.append("POST")
            if effect is not None:
                return effect(*_args, **_kwargs)
            return {
                "status": "completed",
                "verification_request_basis": "c" * 64,
                "verification_post_accepted": {
                    "request_basis": "c" * 64,
                    "mutation_kind": "workflow_dispatch",
                },
            }

        receipt = closer_queue.run_round(
            action_state,
            [target],
            observe,
            execute,
            owner="runner",
            deadline=None,
            receipt_reserve_seconds=0,
            max_operation_retries=0,
            finite_frontier=frontier,
            frontier_target=target,
            request_timeout_seconds=10,
            max_passes=1,
            claim_fn=lambda *_: True,
            release_fn=lambda *_: True,
            holder_fn=lambda *_: None,
        )
        row = receipt["accounting"][0]
        evidence = {
            "target": target,
            "event_id": "c" * 64,
            "operation_id": row["operation_id"],
            "head": binding["head"],
            "merge": binding["merge"],
            "observed_revision": "rev-1",
        }
        if change_evidence:
            evidence.update(change_evidence)
        retained.update(path=action_state, receipt=receipt, evidence=evidence)
        return {
            "status": "pending",
            "pending_kind": "external_event",
            "next_trigger": "authenticated verifier result",
            "pending_evidence": evidence,
            "queue_receipt": receipt,
        }

    path = pipeline_path or tmp_path / "accepted.json"
    result = _run(
        path,
        target,
        frontier,
        now=time.time,
        act=act,
        prepare=lambda *_: {
            "revision": "rev-1",
            "snapshot": {"target": target, "head": binding["head"], "merge": binding["merge"]},
        },
    )
    return path, target, frontier, posts, retained, result


def test_completed_verification_post_wait_is_a_proven_hold_not_unknown(tmp_path):
    path, target, frontier, posts, retained, result = _accepted_request_round(tmp_path)
    row = result["targets"][target]
    assert posts == ["POST"]
    assert row["status"] == "held" and row["deferred_reason"] == "accepted_verifier_request"
    assert result["valid"] and result["stop_valid"]
    checked = cp.validate_stop(path, [target], finite_frontier=frontier, frontier_target=target)
    assert checked["valid"] and checked["stop_valid"]
    # Re-entering the same frontier cannot replay the already accepted POST.
    again = _run(
        path,
        target,
        frontier,
        now=time.time,
        act=lambda *_: pytest.fail("accepted POST must not replay"),
    )
    assert again["targets"][target]["status"] == "held" and posts == ["POST"]


@pytest.mark.parametrize(
    "change",
    [
        {"operation_id": "other-operation"},
        {"event_id": "d" * 64},
        {"target": "acme/repo#43"},
        {"head": "e" * 40},
        {"merge": "f" * 40},
        {"observed_revision": "other-revision"},
    ],
)
def test_accepted_request_wait_rejects_mismatched_effect_binding(tmp_path, change):
    _path, target, _frontier, posts, _retained, result = _accepted_request_round(
        tmp_path, change_evidence=change
    )
    assert posts == ["POST"]
    assert result["targets"][target]["status"] == "unknown"
    assert not result["valid"]


def test_finite_exception_uncertainty_never_advances_to_action(tmp_path):
    target = "acme/repo#6"
    frontier = _frontier(target)
    action_calls = []
    receipt = _run(
        tmp_path / "exception-uncertain.json",
        target,
        frontier,
        now=lambda: 6_000.0,
        interpret=lambda *_: {"substantive_needs": ["needs review"]},
        exception=lambda *_: {
            "status": "unknown",
            "substantive_needs": [],
            "provider_outcome_uncertain": {"kind": "possibly_submitted"},
        },
        act=lambda *_: action_calls.append("act")
        or {"status": "completed", "target_complete": True},
    )
    assert action_calls == []
    assert receipt["targets"][target]["status"] == "unknown"


@pytest.mark.parametrize(
    ("row_status", "row_reason"),
    [("observed", None), ("deferred", "pass_limit")],
)
def test_verified_partial_action_progress_is_held_and_never_replayed(
    tmp_path, monkeypatch, row_status, row_reason
):
    import hashlib
    import json

    import closer_queue

    target = "acme/repo#7"
    frontier = _frontier(target)
    path = tmp_path / "partial-progress.json"
    queue_receipt = {
        "frontier_id": frontier["frontier_id"],
        "accounting": [
            {
                "target": target,
                "revision": "rev-2",
                "action": "review",
                "eligible": True,
                "status": row_status,
                "operation_id": "operation-1",
                "operation_status": "completed",
                **({"reason": row_reason} if row_reason else {}),
            }
        ],
        "events": [
            {
                "kind": "operation_submitted",
                "target": target,
                "operation_id": "operation-1",
                "action": "merge",
            },
            {
                "kind": "action_advanced",
                "target": target,
                "action": "merge",
                "revision": "rev-2",
            },
        ],
        "stop_valid": True,
    }
    progress_key = hashlib.sha256(
        json.dumps(
            {
                "revision": "rev-2",
                "action": "review",
                "operation_id": "operation-1",
                "operation_status": "completed",
                "events": queue_receipt["events"][-8:],
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    validate_calls = []

    def validate(path_arg, assignment, *, receipt, frontier_id):
        validate_calls.append((path_arg, assignment, receipt, frontier_id))
        assert receipt == queue_receipt
        assert assignment == [target]
        assert frontier_id == frontier["frontier_id"]
        return {"valid": True, "stop_valid": True}

    monkeypatch.setattr(closer_queue, "validate_stop", validate)
    monkeypatch.setattr(
        closer_queue,
        "_read_state",
        lambda _path: {
            "frontiers": {
                frontier["frontier_id"]: {
                    "policy": frontier,
                    "items": {
                        target: {
                            "observation_freshness": "fresh",
                            "revision": "rev-2",
                            "action": "review",
                            "eligible": True,
                            "operation": {
                                "id": "operation-1",
                                "status": "completed",
                                "revision": "rev-1",
                                "action": "merge",
                                "binding": {"pr_target": target, "source_targets": []},
                                "finite_frontier": frontier,
                            },
                        }
                    },
                    "round": {},
                }
            }
        },
    )
    calls = []
    act_result = {
        "status": "completed",
        "target_complete": False,
        "progress_key": progress_key,
        "queue_receipt": queue_receipt,
    }
    first = _run(
        path,
        target,
        frontier,
        now=lambda: 7_000.0,
        act=lambda *_: calls.append("act") or act_result,
    )
    for _ in range(5):
        _run(
            path,
            target,
            frontier,
            now=lambda: 7_000.0,
            act=lambda *_: calls.append("act") or act_result,
        )

    row = first["targets"][target]
    assert calls == ["act"]
    assert row["status"] == "held"
    assert row["stage_status"] == "completed"
    assert row["progress_key"] == progress_key
    assert row["results"]["act"]["queue_receipt"] == queue_receipt
    assert row["deferred_next_trigger"] == "new_frontier_required"
    assert first["valid"] and first["stop_valid"]
    assert len(validate_calls) == 1


def test_partial_action_without_exact_verified_queue_receipt_stays_unknown(tmp_path, monkeypatch):
    import closer_queue

    target = "acme/repo#8"
    frontier = _frontier(target)
    monkeypatch.setattr(
        closer_queue,
        "validate_stop",
        lambda *_args, **_kwargs: {"valid": False, "stop_valid": False},
    )
    result = _run(
        tmp_path / "unverified-partial.json",
        target,
        frontier,
        now=lambda: 8_000.0,
        act=lambda *_: {
            "status": "completed",
            "target_complete": False,
            "progress_key": "claimed-progress",
            "queue_receipt": {"stop_valid": True},
        },
    )
    assert result["targets"][target]["status"] == "unknown"
    assert not result["valid"]


@pytest.mark.parametrize("stage", ["interpret", "exception"])
def test_finite_model_transport_loss_is_unknown_and_not_replayed(tmp_path, stage):
    target = f"acme/repo#{8 if stage == 'interpret' else 9}"
    frontier = _frontier(target)
    calls = []

    def interpret(*_args):
        if stage == "interpret":
            calls.append("interpret")
            raise TimeoutError("provider may have started")
        return {"substantive_needs": ["needs review"]}

    def exception(*_args):
        if stage == "exception":
            calls.append("exception")
            raise TimeoutError("provider may have started")
        return {"route": "normal"}

    path = tmp_path / f"{stage}-timeout.json"
    first = _run(
        path, target, frontier, now=lambda: 8_000.0, interpret=interpret, exception=exception
    )
    second = _run(
        path, target, frontier, now=lambda: 8_001.0, interpret=interpret, exception=exception
    )
    assert calls == [stage]
    assert first["targets"][target]["status"] == "unknown"
    assert second["targets"][target]["status"] == "unknown"


def test_finite_state_lock_timeout_after_action_intent_never_replays(tmp_path):
    target = "acme/repo#7"
    path = tmp_path / "locked-state.json"
    frontier, timeout = _frontier_with_timeout(target)
    calls = []
    holder_ready = multiprocessing.Event()
    holder_release = multiprocessing.Event()
    holder = None

    def act(*_args):
        nonlocal holder
        calls.append("act")
        holder = multiprocessing.Process(
            target=_hold_pipeline_lock, args=(path, holder_ready, holder_release)
        )
        holder.start()
        assert holder_ready.wait(timeout=2)
        return {"status": "completed", "target_complete": True}

    try:
        receipt = cp._run_pipeline_finite(
            path,
            [target],
            frontier_target=target,
            finite_frontier=frontier,
            request_timeout_seconds=timeout,
            capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
            prepare=lambda _target, _ctx: {"revision": "rev-1"},
            interpret=lambda _target, _ctx: {"substantive_needs": []},
            exception=lambda _target, _ctx: {"route": "normal"},
            act=act,
            now=time.time,
        )
        assert holder is not None
        # Prove the request returns while the other process still owns the lock.
        # A wall-clock cutoff also counts unrelated CI process scheduling delay.
        assert holder.is_alive()
    finally:
        holder_release.set()
        if holder is not None:
            holder.join(timeout=2)
    assert holder is not None and holder.exitcode == 0
    assert calls == ["act"]
    assert receipt["targets"][target]["status"] == "unknown"
    assert receipt["stop_valid"] is False

    restarted = _run(path, target, frontier, now=time.time, act=act)
    assert calls == ["act"]
    assert restarted["targets"][target]["status"] == "unknown"


def test_finite_pipeline_rejects_foreign_stored_target_before_callbacks(tmp_path):
    target = "acme/repo#selected"
    foreign = "acme/repo#foreign"
    frontier = _frontier(target)
    path = tmp_path / "foreign-state.json"
    run_id = cp._digest(["finite-frontier/v2", frontier["frontier_id"], frontier["owner"]])[:32]
    state = cp._empty()
    state["frontiers"][frontier["frontier_id"]] = {
        "schema": "closer-pipeline-frontier/v2",
        "run_id": run_id,
        "finite_frontier": frontier,
        "items": {foreign: {"status": "unknown", "stage_status": "unknown"}},
    }
    cp._save(path, state)
    calls = []
    try:
        _run(
            path,
            target,
            frontier,
            now=time.time,
            prepare=lambda *_: calls.append("prepare") or {"revision": "rev-1"},
        )
    except ValueError as exc:
        assert "foreign target" in str(exc)
    else:
        raise AssertionError("foreign frontier target was accepted")
    assert calls == []
