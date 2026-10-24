"""Whole queue/pipeline receipt recovery: a known POST is never replayed."""

import copy
import fcntl
import json
import threading
import time
from pathlib import Path

import pytest
from test_closer_pipeline_frontier import _accepted_request_round

import closer_finite_reentry as recovery
import closer_pipeline as pipeline
import closer_queue as queue
import closer_resources as resources
import closer_stage_continuation as continuation
from closer_frontier_policy import action_identity, make_frontier


def case(tmp_path):
    target = "acme/repo#42"
    root = tmp_path / "queue.pipeline.json.scoped"
    path = resources.shard_dir(root, target) / "state.json"
    path, target, frontier, posts, inner, _result = _accepted_request_round(
        tmp_path, pipeline_path=path
    )
    inner["path"] = Path(inner["path"])
    state = pipeline._load(path)
    state["runs"]["unrelated-history"] = {"retained": True}
    row = state["frontiers"][frontier["frontier_id"]]["items"][target]
    # Retain the actual legacy callback/queue result and reproduce only its
    # faulty outer classification, as produced before this repair.
    row.update(
        status="unknown",
        error="action wait lacks exact verified no-effect queue proof",
        deferred_next_trigger="manual_reconciliation",
    )
    row.pop("deferred_reason", None)
    row.pop("resource_evidence", None)
    row.pop("queue_receipt", None)
    row["scope_lease"] = {"owner": "runner", "target": target, "epoch": 1}
    pipeline._save(path, state)
    (path.parent / "lease.json").write_text(
        json.dumps({"owner": "runner", "target": target, "epoch": 1, "finite_frontier": frontier})
    )
    return path, target, frontier, posts, inner, copy.deepcopy(row)


def reconcile(path, target, frontier):
    plan = recovery.plan(path, frontier=frontier, target=target, event="accepted-result-observed")
    result = recovery.apply(
        path,
        frontier=frontier,
        target=target,
        event="accepted-result-observed",
        expected_plan_sha256=plan["plan_sha256"],
    )
    return plan, result


def test_audited_accepted_post_recovery_preserves_effect_and_never_requeues(tmp_path):
    path, target, frontier, posts, inner, before = case(tmp_path)
    original_queue = inner["path"].read_bytes()
    plan, result = reconcile(path, target, frontier)
    assert plan["kind"] == "accepted_verifier_request_receipt"
    assert result["status"] == "applied"
    state = pipeline._load(path)
    after = state["frontiers"][frontier["frontier_id"]]["items"][target]
    assert after["status"] == "held" and after["deferred_reason"] == "accepted_verifier_request"
    assert "reentry_ready" not in after and "error" not in after
    assert after["results"] == before["results"]
    assert after["reentry_history"][0]["previous"] == before
    assert state["runs"]["unrelated-history"] == {"retained": True}
    assert inner["path"].read_bytes() == original_queue and posts == ["POST"]
    assert pipeline.validate_stop(path, [target], finite_frontier=frontier, frontier_target=target)[
        "valid"
    ]
    again = recovery.apply(
        path,
        frontier=frontier,
        target=target,
        event="accepted-result-observed",
        expected_plan_sha256=plan["plan_sha256"],
    )
    assert again["idempotent"] and posts == ["POST"]


@pytest.mark.parametrize(
    "corrupt",
    [
        "operation",
        "request_basis",
        "queue_receipt",
        "frontier",
        "uncertain_flag",
        "no_effect",
        "uncertain_string",
        "uncertain_object",
        "no_effect_number",
        "advisory_object",
    ],
)
def test_recovery_rejects_unbound_or_uncertain_effect_without_writes(tmp_path, corrupt):
    path, target, frontier, _posts, inner, _before = case(tmp_path)
    document = json.loads(inner["path"].read_text())
    bucket = document["frontiers"][frontier["frontier_id"]]
    operation = bucket["items"][target]["operation"]
    if corrupt == "operation":
        operation["status"] = "uncertain"
    elif corrupt == "request_basis":
        operation["result"]["verification_request_basis"] = "d" * 64
    elif corrupt == "queue_receipt":
        bucket["round"]["receipt"]["stop_valid"] = False
    elif corrupt == "uncertain_flag":
        operation["result"]["outcome_uncertain"] = True
    elif corrupt == "no_effect":
        operation["result"]["no_effect"] = True
    elif corrupt == "uncertain_string":
        operation["result"]["outcome_uncertain"] = "unknown"
    elif corrupt == "uncertain_object":
        operation["outcome_uncertain"] = {}
    elif corrupt == "no_effect_number":
        operation["result"]["no_effect"] = 0
    elif corrupt == "advisory_object":
        operation["result"]["advisory_only"] = {}
    else:
        operation["finite_frontier"] = {**frontier, "frontier_id": "foreign"}
    inner["path"].write_text(json.dumps(document))
    original = path.read_bytes()
    with pytest.raises(ValueError, match="not_authenticated"):
        recovery.plan(path, frontier=frontier, target=target, event="observed")
    assert path.read_bytes() == original


def test_recovery_cas_and_live_owner_lock_do_not_change_unknown_row(tmp_path):
    path, target, frontier, _posts, inner, _before = case(tmp_path)
    plan = recovery.plan(path, frontier=frontier, target=target, event="observed")
    with path.with_name(path.name + ".runlock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(resources.ResourceBlocked, match="active_finite_writer"):
            recovery.apply(
                path,
                frontier=frontier,
                target=target,
                event="observed",
                expected_plan_sha256=plan["plan_sha256"],
            )
    document = json.loads(inner["path"].read_text())
    document["new_unrelated_metadata"] = True
    inner["path"].write_text(json.dumps(document))
    with pytest.raises(ValueError, match="plan_changed"):
        recovery.apply(
            path,
            frontier=frontier,
            target=target,
            event="observed",
            expected_plan_sha256=plan["plan_sha256"],
        )
    assert (
        pipeline._load(path)["frontiers"][frontier["frontier_id"]]["items"][target]["status"]
        == "unknown"
    )


@pytest.mark.parametrize("duplicate_frontier", [False, True, "completed_read"])
def test_recovered_request_uses_normal_causal_successor_after_fresh_result(
    tmp_path, duplicate_frontier
):
    path, target, frontier, posts, inner, _before = case(tmp_path)
    reconcile(path, target, frontier)
    if duplicate_frontier:
        read_calls = []
        action = "collect_evidence" if duplicate_frontier == "completed_read" else "wait"
        binding = {"pr_target": target, "source_targets": [], "head": "a" * 40, "merge": "b" * 40}
        frontier = make_frontier(
            [target],
            owner=frontier["owner"],
            frontier_id="later-duplicate-wait",
            bindings={
                target: {
                    "revision": "rev-1",
                    "action_id": action_identity(target, "rev-1", action, binding),
                }
            },
        )
        receipt = queue.run_round(
            inner["path"],
            [target],
            lambda _t: {
                "target": target,
                "revision": "rev-1",
                "eligible": duplicate_frontier == "completed_read" and not read_calls,
                "action": action if not read_calls else "wait",
                "reason": (
                    "read current evidence"
                    if duplicate_frontier == "completed_read" and not read_calls
                    else "same verification request already accepted"
                ),
                "next_trigger": "authenticated verifier result",
                "operation_binding": binding,
                "evidence": {"target": target, **binding},
            },
            lambda *_: read_calls.append("read") or {"status": "completed"},
            finite_frontier=frontier,
            frontier_target=target,
            deadline=None,
            request_timeout_seconds=10,
            receipt_reserve_seconds=0,
            max_operation_retries=0,
            claim_fn=lambda *_: True,
            release_fn=lambda *_: True,
            holder_fn=lambda *_: None,
        )
        state = pipeline._load(path)
        original = next(iter(state["frontiers"].values()))["items"][target]
        item = copy.deepcopy(original)
        item["results"]["act"]["queue_receipt"] = receipt
        item["queue_receipt"] = receipt
        proof = queue.accepted_verifier_wait_proof(
            inner["path"],
            target,
            frontier=frontier,
            receipt=receipt,
            evidence=item["results"]["act"]["pending_evidence"],
        )
        assert proof and proof["operation_frontier"] != frontier["frontier_id"]
        if duplicate_frontier == "completed_read":
            assert read_calls == ["read"]
            assert proof["receipt_operation_id"] != proof["operation_id"]
        item["resource_evidence"] = {"accepted_verifier_request": proof}
        state["frontiers"][frontier["frontier_id"]] = {
            "schema": "closer-pipeline-frontier/v2",
            "finite_frontier": frontier,
            "run_id": "duplicate-wait-run",
            "items": {target: item},
        }
        pipeline._save(path, state)
    canonical = tmp_path / "canonical.json"
    assignment = {"target": target, "fingerprint": "frozen-original"}
    canonical.write_text(json.dumps({"targets": [assignment]}))
    observed_at = time.time()
    observation = {
        "target": target,
        "revision": "fresh-result-revision",
        "action": "collect_evidence",
        "eligible": True,
        "observed_at": observed_at,
        "operation_binding": {
            "pr_target": target,
            "source_targets": [],
            "head": "a" * 40,
            "merge": "b" * 40,
        },
        "evidence": {"verifier_lifecycle": {"verdict": "NON_PASS", "run_id": "123"}},
    }
    result = continuation.create_successor(
        path,
        inner["path"],
        predecessor=frontier,
        target=target,
        observation=observation,
        canonical_path=canonical,
        canonical_row=assignment,
        admission=lambda: {"paused": False, "dependency": None, "live_owner": False},
        now=lambda: observed_at + 1,
    )
    assert result["execution_targets"] == [target] and posts == ["POST"]
    successor = pipeline._load(path)["stage_successors"][result["finite_frontier"]["frontier_id"]]
    assert "items" not in successor and "results" not in successor


@pytest.mark.parametrize(
    "malformed", ["resource", "results", "act", "changed_resource", "act_not_pending"]
)
def test_malformed_accepted_wait_resource_proof_refuses_stop_without_crashing(tmp_path, malformed):
    path, target, frontier, _posts, _inner, _before = case(tmp_path)
    reconcile(path, target, frontier)
    state = pipeline._load(path)
    row = state["frontiers"][frontier["frontier_id"]]["items"][target]
    if malformed == "resource":
        row["resource_evidence"] = ["bad"]
    elif malformed == "results":
        row["results"] = None
    elif malformed == "act":
        row["results"]["act"] = None
    elif malformed == "changed_resource":
        row["resource_evidence"]["accepted_verifier_request"]["operation_id"] = "foreign"
    else:
        row["results"]["act"]["status"] = "completed"
    pipeline._save(path, state)
    assert not pipeline.validate_stop(
        path, [target], finite_frontier=frontier, frontier_target=target
    )["valid"]
    if malformed in {"changed_resource", "act_not_pending"}:
        canonical = tmp_path / "canonical-reject.json"
        assignment = {"target": target, "fingerprint": "frozen"}
        canonical.write_text(json.dumps({"targets": [assignment]}))
        observed_at = time.time()
        observation = {
            "target": target,
            "revision": "fresh-result",
            "action": "collect_evidence",
            "eligible": True,
            "observed_at": observed_at,
            "operation_binding": {
                "pr_target": target,
                "source_targets": [],
                "head": "a" * 40,
                "merge": "b" * 40,
            },
            "evidence": {"verifier_lifecycle": {"verdict": "NON_PASS", "run_id": "123"}},
        }
        expected = (
            "proof differs" if malformed == "changed_resource" else "not completed partial progress"
        )
        before = path.read_bytes()
        with pytest.raises(ValueError, match=expected):
            continuation.create_successor(
                path,
                _inner["path"],
                predecessor=frontier,
                target=target,
                observation=observation,
                canonical_path=canonical,
                canonical_row=assignment,
                admission=lambda: {"paused": False, "dependency": None, "live_owner": False},
                now=lambda: observed_at + 1,
            )
        assert path.read_bytes() == before


def test_inner_queue_is_locked_through_outer_commit_audit_and_retirement(tmp_path, monkeypatch):
    path, target, frontier, _posts, inner, _before = case(tmp_path)
    original_save = pipeline._save_snapshot
    original_retire = recovery._retire_native_boundary
    observed = []

    def verify_other_writer_is_excluded(stage):
        available = []

        def contender():
            with queue._ledger_lock(
                inner["path"], cutoff=time.time() + 0.03, now=time.time
            ) as locked:
                available.append(locked)

        thread = threading.Thread(target=contender)
        thread.start()
        thread.join(timeout=2)
        assert available == [False], stage
        # Same-thread stop validation safely reuses the actual held lock.
        with queue._ledger_lock(inner["path"], cutoff=time.time() + 0.03, now=time.time) as nested:
            assert nested
        observed.append(stage)

    def save(*args, **kwargs):
        verify_other_writer_is_excluded("outer_commit")
        return original_save(*args, **kwargs)

    def retire(*args, **kwargs):
        verify_other_writer_is_excluded("retirement")
        return original_retire(*args, **kwargs)

    monkeypatch.setattr(pipeline, "_save_snapshot", save)
    monkeypatch.setattr(recovery, "_retire_native_boundary", retire)
    reconcile(path, target, frontier)
    assert observed == ["outer_commit", "retirement"]
