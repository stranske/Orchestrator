"""Caller composition using inert durable operation fixtures, never live effects."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from test_closer_stage_continuation import case as _successor_case

import closer_dispatcher as dispatcher
import closer_frontier_policy as policy
import closer_pipeline as pipeline
import closer_resources as resources
import closer_stage_continuation as continuation


@pytest.fixture
def successor_case(tmp_path, monkeypatch):
    return _successor_case.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def caller(successor_case, tmp_path, monkeypatch):
    _, action_path, source_args, qstate, document = successor_case
    target = source_args["target"]
    binding = {"pr_target": target, "source_targets": [], "head": "exact-head"}
    predecessor = policy.make_frontier_from_observations(
        {
            target: {
                "target": target,
                "revision": "r1",
                "action": "merge",
                "eligible": True,
                "operation_binding": binding,
            }
        },
        owner="frozen-cohort",
        frontier_id="predecessor",
    )
    document["frontiers"]["predecessor"]["finite_frontier"] = predecessor
    bucket = qstate["frontiers"]["predecessor"]
    bucket["policy"] = predecessor
    bucket["round"]["finite_frontier"] = predecessor
    operation = bucket["items"][target]["operation"]
    operation.update(binding=binding, finite_frontier=predecessor)
    raw_state = tmp_path / "native-queue.json"
    root_path = dispatcher._pipeline_state_path(raw_state)
    shard = resources.shard_dir(resources.scoped_root(root_path), target) / "state.json"
    shard.parent.mkdir(parents=True)
    pipeline._save(shard, document)
    action = shard.parent / "state.actions" / f"{pipeline._digest(target)}.json"
    action.parent.mkdir()
    action.write_text(json.dumps(qstate))
    args = SimpleNamespace(
        finite_frontier=predecessor,
        shadow=False,
        coordinator_mode="resource_scoped",
        state=str(raw_state),
        assignment=source_args["canonical_path"],
        request_timeout_seconds=120,
    )
    observed = {
        **source_args["observation"],
        "state": "MERGED",
        "head": "exact-head",
        "merge": "merge-sha",
        "action": "post_merge_verify",
        "operation_binding": binding,
    }
    calls = []
    backend = SimpleNamespace(
        gh=SimpleNamespace(deadline=0),
        local_first=True,
        assignment_rows={target: source_args["canonical_row"]},
    )

    def observe(name):
        assert backend.local_first is False and backend.gh.deadline > 0
        calls.append(("observe", name))
        return copy.deepcopy(observed)

    backend.observe = observe
    real_helper = continuation.create_successor

    def create(*positional, **kwargs):
        calls.append(("successor", kwargs["target"]))
        return real_helper(*positional, **kwargs)

    monkeypatch.setattr(continuation, "create_successor", create)

    def run(child_args, targets, child_backend, deadline):
        assert child_backend is backend and deadline is None
        policy.validate_frontier(child_args.finite_frontier, targets=targets)
        assert targets == [target]
        calls.append(("normal_pipeline", targets))
        return {"round_id": "child-round", "valid": True}

    monkeypatch.setattr(dispatcher, "_run_staged_pipeline", run)
    monkeypatch.setattr(
        pipeline, "validate_stop_scoped", lambda *_a: {"valid": True, "stop_valid": True}
    )
    result = {"validation": {"valid": True, "stop_valid": True}, "round_id": "original-round"}
    return SimpleNamespace(
        args=args,
        backend=backend,
        result=result,
        target=target,
        calls=calls,
        observation=observed,
        shard=shard,
        action=action,
        qstate=qstate,
        document=document,
    )


def invoke(caller):
    return dispatcher._run_merge_verification_successors(
        caller.args, caller.backend, caller.result, [caller.target]
    )


def test_confirmed_merge_fresh_same_head_reenters_normal_pipeline(caller):
    before = copy.deepcopy(caller.document["frontiers"]["predecessor"])
    result = invoke(caller)
    assert caller.calls == [
        ("observe", caller.target),
        ("successor", caller.target),
        ("normal_pipeline", [caller.target]),
    ]
    continuation_result = result["merge_verification_successors"][caller.target]
    assert continuation_result["status"] == "verification_successor"
    assert continuation_result["native_result"]["validation"]["valid"]
    assert pipeline._load(caller.shard)["frontiers"]["predecessor"] == before


@pytest.mark.parametrize(
    "change",
    [
        "uncompleted_merge",
        "different_operation",
        "not_merged",
        "wrong_head",
        "active_verifier",
        "verify_label",
        "pause",
        "assignment_drift",
        "live_lease",
        "read_failure",
    ],
)
def test_nonactionable_or_unproven_cases_never_dispatch_verification(caller, change):
    if change in {"uncompleted_merge", "different_operation"}:
        op = caller.qstate["frontiers"]["predecessor"]["items"][caller.target]["operation"]
        op["status" if change == "uncompleted_merge" else "action"] = (
            "uncertain" if change == "uncompleted_merge" else "review"
        )
        caller.action.write_text(json.dumps(caller.qstate))
    elif change == "not_merged":
        caller.observation["state"] = "OPEN"
    elif change == "wrong_head":
        caller.observation["head"] = "foreign-head"
    elif change in {"active_verifier", "verify_label"}:
        caller.observation.update(action="wait", reason=change, next_trigger="verifier_terminal")
    elif change == "pause":
        caller.observation["eligible"] = False
    elif change == "assignment_drift":
        caller.args.assignment.write_text(
            json.dumps(
                {
                    "targets": [
                        {
                            "target": caller.target,
                            "fingerprint": "changed",
                            "execution_state": "held",
                        }
                    ]
                }
            )
        )
    elif change == "read_failure":
        caller.backend.observe = lambda *_a: (_ for _ in ()).throw(OSError("read failed"))
    if change == "live_lease":
        import fcntl

        with (caller.shard.parent / "lease.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            invoke(caller)
    else:
        invoke(caller)
    assert not any(call[0] == "normal_pipeline" for call in caller.calls)
    assert "stage_successors" not in pipeline._load(caller.shard)


def test_child_invalid_validation_propagates_to_overall_result(caller, monkeypatch):
    monkeypatch.setattr(
        pipeline,
        "validate_stop_scoped",
        lambda *_a: {"valid": False, "stop_valid": False, "reasons": ["child_unknown"]},
    )
    result = invoke(caller)
    assert result["validation"]["valid"] is False
    assert result["validation"]["stop_valid"] is False
    assert result["validation"]["continuation_required"] is True


def test_child_round_failure_does_not_launder_predecessor_validity(caller, monkeypatch):
    monkeypatch.setattr(
        dispatcher,
        "_run_staged_pipeline",
        lambda *_a: {"round_id": None, "valid": False, "continuation_required": True},
    )
    monkeypatch.setattr(
        pipeline,
        "validate_stop_scoped",
        lambda *_a: {"valid": False, "stop_valid": False, "reasons": ["missing_child_round"]},
    )
    result = invoke(caller)
    assert result["validation"]["valid"] is False
    assert result["validation"]["continuation_required"] is True


def test_active_assignment_fingerprint_metadata_churn_uses_normal_native_admission(caller):
    caller.args.assignment.write_text(
        json.dumps(
            {
                "targets": [
                    {"target": caller.target, "lane": "closer", "fingerprint": "metadata-refreshed"}
                ]
            }
        )
    )
    invoke(caller)
    assert any(call[0] == "normal_pipeline" for call in caller.calls)
