"""Caller composition using inert durable operation fixtures, never live effects."""

from __future__ import annotations

import copy
import inspect
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_closer_reasoning_boundary import native_world as native_world
from test_closer_stage_continuation import case as _successor_case

import closer_dispatcher as dispatcher
import closer_frontier_policy as policy
import closer_pipeline as pipeline
import closer_queue as queue
import closer_resources as resources
import closer_stage_continuation as continuation

_REAL_STAGED_PIPELINE = dispatcher._run_staged_pipeline
_REAL_VALIDATE_STOP = queue.validate_stop
_REAL_RUN_ROUND = queue.run_round


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


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "unchanged_action",
        "missing_advance",
        "foreign_advance",
        "missing_submitted",
        "foreign_submitted",
        "wrong_operation_id",
    ],
    ids=[
        "valid-r1-r2",
        "unchanged-action",
        "missing-advance",
        "foreign-advance",
        "missing-submitted",
        "foreign-submitted",
        "wrong-operation-id",
    ],
)
def test_real_queue_intermediate_and_final_revisions_dispatch_verification(
    caller, monkeypatch, mutation
):
    """Native validator + actual durable queue, with only external effects inert."""
    target = caller.target
    frontier = caller.args.finite_frontier
    binding = {"pr_target": target, "source_targets": [], "head": "exact-head"}
    state = {"merged": False, "reads_after_merge": 0, "effects": 0}

    def observe(_target):
        revision, action = "r1", "merge"
        if state["merged"]:
            state["reads_after_merge"] += 1
            revision = "post-merge-first" if state["reads_after_merge"] == 1 else "post-merge-final"
            action = "post_merge_verify"
        return {
            "target": target,
            "revision": revision,
            "action": action,
            "eligible": True,
            "operation_binding": binding,
        }

    def execute(_target, action, _observation, operation):
        assert action == "merge" and operation["finite_frontier"] == frontier
        state["effects"] += 1
        state["merged"] = True
        return {"status": "completed", "advanced": True, "result": {"merge_sha": "merged-sha"}}

    caller.action.unlink()
    monkeypatch.setattr(queue, "validate_stop", _REAL_VALIDATE_STOP)
    receipt = queue.run_round(
        caller.action,
        [target],
        observe,
        execute,
        deadline=None,
        finite_frontier=frontier,
        frontier_target=target,
        receipt_reserve_seconds=0,
        max_operation_retries=0,
        max_workers=1,
        max_passes=1,
        claim_fn=lambda *_: True,
        release_fn=lambda *_: True,
        holder_fn=lambda *_: None,
    )
    row = receipt["accounting"][0]
    advanced = next(e for e in receipt["events"] if e["kind"] == "action_advanced")
    assert advanced["revision"] == "post-merge-first"
    assert row["revision"] == "post-merge-final" and row["action"] == "post_merge_verify"
    assert receipt["stop_valid"] and row["reason"] == "pass_limit" and state["effects"] == 1

    if mutation:
        if mutation == "unchanged_action":
            row["action"] = "merge"
        elif mutation.startswith("missing_"):
            kind = "action_advanced" if mutation == "missing_advance" else "operation_submitted"
            receipt["events"] = [event for event in receipt["events"] if event["kind"] != kind]
        else:
            kind = "action_advanced" if mutation == "foreign_advance" else "operation_submitted"
            event = next(event for event in receipt["events"] if event["kind"] == kind)
            if mutation == "wrong_operation_id":
                event["operation_id"] = "another-operation"
            else:
                event["target"] = "foreign/repo#99"
        # Preserve exact durable receipt readback; negatives exercise witness checks,
        # rather than merely failing a mismatched supplied/stored receipt.
        data = queue._read_state(caller.action)
        bucket = data["frontiers"][frontier["frontier_id"]]
        bucket["round"]["receipt"] = copy.deepcopy(receipt)
        if mutation == "unchanged_action":
            bucket["items"][target]["action"] = "merge"
        caller.action.write_text(json.dumps(data))

    captured = {}

    class CapturedCallbacks(Exception):
        pass

    def capture_pipeline(callbacks):
        captured.update(callbacks)
        raise CapturedCallbacks

    native_backend = dispatcher.Closer(
        SimpleNamespace(),
        state_path=caller.args.state,
        assignment_rows=caller.backend.assignment_rows,
        workspaces={},
        apply=True,
        workers=True,
    )
    caller.args.max_workers = 1
    with monkeypatch.context() as context:
        # Stop before registration/admission; inspect an actual production closure only.
        context.setattr(pipeline, "_register_dispatcher_finite_callbacks", capture_pipeline)
        context.setattr(dispatcher, "_run_staged_pipeline", _REAL_STAGED_PIPELINE)
        with pytest.raises(CapturedCallbacks):
            _REAL_STAGED_PIPELINE(caller.args, [target], native_backend, None)
    # Capture the actual registered production callback closure, not a copied predicate.
    validator = inspect.getclosurevars(captured["act"]).nonlocals[
        "verified_finite_partial_progress"
    ]
    progress = validator(target, caller.action, row, receipt)
    if mutation:
        assert progress is None
        assert state["effects"] == 1
        return
    assert (
        progress is not None
    ), "completed native merge must survive final observation revision drift"
    assert progress["status"] == "completed" and progress["target_complete"] is False
    original = pipeline._load(caller.shard)
    item = original["frontiers"]["predecessor"]["items"][target]
    item.update(
        status="held",
        stage="act",
        stage_status="completed",
        deferred_next_trigger="new_frontier_required",
        progress_key=progress["progress_key"],
    )
    item["results"]["act"] = progress
    pipeline._save(caller.shard, original)
    result = invoke(caller)
    assert result["merge_verification_successors"][target]["status"] == "verification_successor"
    assert ("normal_pipeline", [target]) in caller.calls
    assert state["effects"] == 1, "successor must not repeat the merge"


@pytest.fixture
def public_merge_world(native_world, monkeypatch):
    """Real public CLI/registered callbacks/queue; fake external GitHub/guard only."""
    import closer_dispatcher as native

    w = native_world
    w.ad.unlink()
    w.snapshot.update(state="OPEN", merge=None, verifier=None)
    w.originals.pop(w.ad)
    monkeypatch.setattr(queue, "run_round", _REAL_RUN_ROUND)
    transport = native.gh_capacity.gh_run
    w.merges = []
    w.verifies = []
    w.guard_mode = "merge"

    def github_transport(args, **kwargs):
        endpoint = args[3]
        if "/actions/workflows/agents-verifier.yml/runs" in endpoint:
            return 0, {"total_count": 0, "workflow_runs": []}
        if endpoint == "repos/owner/repo":
            return 0, {"default_branch": "main"}
        if endpoint.endswith("/issues/12/labels") or endpoint.endswith(
            "/actions/workflows/agents-verifier.yml/dispatches"
        ):
            assert args[:3] == ["api", "--method", "POST"]
            w.verifies.append(tuple(args))
            w.snapshot["labels"] = ["verify:compare"]
            w.snapshot["revision"] = "verify-requested"
            w.snapshot["verifier_history_complete"] = True
            w.snapshot["verifier_run_identity_unknown"] = False
            w.snapshot["verifier_runs"] = [
                {
                    "id": 42,
                    "status": "in_progress",
                    "conclusion": None,
                    "linked_pr": "owner/repo#12",
                    "head_sha": w.snapshot["head"],
                    "path": ".github/workflows/agents-verifier.yml",
                    "event": "pull_request_target",
                    "run_attempt": 1,
                    "created_at": "2026-10-22T00:00:01Z",
                }
            ]
            return 0, [{"name": "verify:compare"}]
        return transport(args, **kwargs)

    def merge_transport(command, *, timeout):
        assert Path(command[1]).name == "merge_guard.py"
        assert "--confirm-merge" in command and "--remote-only" in command
        w.merges.append(command)
        resources.finite_pool_no_start()
        if w.guard_mode == "blocked":
            return SimpleNamespace(
                returncode=1, stdout=json.dumps({"merge_executed": False, "blocked": True})
            )
        if w.guard_mode == "uncertain":
            raise RuntimeError("external guard outcome unavailable")
        w.snapshot.update(
            state="MERGED",
            merge="d" * 40,
            merged_at="2026-10-22T00:00:00Z",
            revision="merged-current",
            verifier_history_complete=True,
        )
        resources.finite_pool_no_start()
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"merge_executed": True, "blocked": False, "merge_sha": "d" * 40}),
        )

    monkeypatch.setattr(native.gh_capacity, "gh_run", github_transport)
    monkeypatch.setattr(native, "bounded_run", merge_transport)
    observer = native.Closer(
        native.GitHub(deadline=time.time() + 25), state_path=w.raw, apply=True, workers=True
    )
    w.frontier = policy.make_frontier_from_observations(
        {"owner/repo#12": observer.observe("owner/repo#12")},
        owner=w.frontier["owner"],
        frontier_id=w.frontier["frontier_id"],
    )
    w.argv[w.argv.index("--finite-frontier-json") + 1] = json.dumps(w.frontier)
    return w


def test_public_finite_main_verifies_selected_merge_despite_foreign_history(
    public_merge_world, capsys
):
    w = public_merge_world
    assert dispatcher.main(w.argv) == 78
    result = json.loads(capsys.readouterr().out)
    assert len(w.merges) == 1, result
    assert len(w.verifies) == 1, result.get("merge_verification_successors", result["validation"])
    assert w.events["provider"] == 0
    assert result["validation"]["valid"] is False
    assert result["validation"]["global_stop_valid"] is False
    assert all(path.read_bytes() == original for path, original in w.originals.items())
    successor = result["merge_verification_successors"]["owner/repo#12"]
    assert successor["status"] == "verification_successor", successor
    native = successor["native_result"]
    assert native["validation"]["scoped_completion_valid"] is True, native
    assert native["validation"]["admitted_domain_valid"] is True, native
    binding = successor["lineage"]["finite_frontier"]["bindings"]["owner/repo#12"]
    assert binding["revision"] != w.frontier["bindings"]["owner/repo#12"]["revision"]


@pytest.mark.parametrize("guard_mode", ["blocked", "uncertain"])
def test_public_main_without_confirmed_merge_never_requests_successor(
    public_merge_world, capsys, guard_mode
):
    w = public_merge_world
    w.guard_mode = guard_mode
    assert dispatcher.main(w.argv) == 78
    result = json.loads(capsys.readouterr().out)
    assert len(w.merges) == 1
    assert w.verifies == [] and w.snapshot["state"] == "OPEN"
    assert w.events["provider"] == 0
    assert "merge_verification_successors" not in result
    assert all(path.read_bytes() == original for path, original in w.originals.items())


@pytest.mark.parametrize(
    "change",
    [
        "nonfinite",
        "legacy",
        "shadow",
        "preview",
        "selected_unknown",
        "domain_unknown",
        "global_integrity",
        "global_missing",
        "global_malformed",
        "quarantine_missing",
        "nonboolean_selected",
    ],
)
def test_successor_caller_requires_exact_finite_selected_integrity(change):
    args = SimpleNamespace(
        pipeline=True,
        finite_frontier={"frontier_id": "frozen"},
        coordinator_mode="resource_scoped",
        shadow=False,
    )
    validation = {
        "valid": False,
        "global_stop_valid": False,
        "scoped_completion_valid": True,
        "admitted_domain_valid": True,
        "historical_quarantine": {"global_reasons": []},
    }
    assert dispatcher._successor_caller_admitted(args, validation) is True
    if change == "nonfinite":
        args.finite_frontier = None
    elif change == "legacy":
        args.coordinator_mode = "legacy"
    elif change == "shadow":
        args.shadow = True
    elif change == "preview":
        args.pipeline = False
    elif change == "selected_unknown":
        validation["scoped_completion_valid"] = False
    elif change == "domain_unknown":
        validation["admitted_domain_valid"] = False
    elif change == "global_integrity":
        validation["historical_quarantine"]["global_reasons"] = ["unreadable_ledger"]
    elif change == "global_missing":
        validation["historical_quarantine"] = {}
    elif change == "global_malformed":
        validation["historical_quarantine"]["global_reasons"] = None
    elif change == "quarantine_missing":
        validation.pop("historical_quarantine")
    else:
        validation["scoped_completion_valid"] = 1
    before = copy.deepcopy(validation)
    assert dispatcher._successor_caller_admitted(args, validation) is False
    assert validation == before


def test_successor_caller_preserves_existing_aggregate_valid_path():
    args = SimpleNamespace(
        pipeline=True, finite_frontier=None, coordinator_mode="legacy", shadow=False
    )
    assert dispatcher._successor_caller_admitted(args, {"valid": True}) is True
    args.pipeline = False
    assert dispatcher._successor_caller_admitted(args, {"valid": True}) is False


@pytest.mark.parametrize(
    "change,refusal",
    [
        ("arguments", "finite successor requires the exact CLI-owned child/backend"),
        ("transport", "finite dispatcher backend contains an unsupported transport override"),
        ("backend", "finite callbacks require the exact CLI-constructed dispatcher backend"),
    ],
)
def test_public_successor_registry_rejects_changed_child_or_backend(
    public_merge_world, monkeypatch, capsys, change, refusal
):
    w = public_merge_world
    original_copy = copy.copy

    class ForeignBackend(dispatcher.Closer):
        pass

    def child_copy(value):
        following = original_copy(value)
        caller_frame = inspect.currentframe().f_back
        if caller_frame.f_code is dispatcher._run_merge_verification_successors.__code__:
            if change == "arguments":
                following.max_workers += 1
            elif change == "transport":
                caller_frame.f_locals["backend"].offload_fn = lambda *_a, **_kw: None
            else:
                caller_frame.f_locals["backend"].__class__ = ForeignBackend
        return following

    monkeypatch.setattr(copy, "copy", child_copy)
    assert dispatcher.main(w.argv) == 78
    result = json.loads(capsys.readouterr().out)
    outcome = result["merge_verification_successors"]["owner/repo#12"]
    assert outcome["status"] == "unknown" and refusal in outcome["reason"], outcome
    assert len(w.merges) == 1 and not w.verifies and w.events["provider"] == 0
    assert all(path.read_bytes() == original for path, original in w.originals.items())


def test_registered_backend_cannot_launch_child_from_external_caller(
    public_merge_world, monkeypatch, capsys
):
    w = public_merge_world
    original_copy = copy.copy
    captured = {}

    def child_copy(value):
        following = original_copy(value)
        caller_frame = inspect.currentframe().f_back
        if caller_frame.f_code is dispatcher._run_merge_verification_successors.__code__:
            captured.update(args=following, backend=caller_frame.f_locals["backend"])
        return following

    monkeypatch.setattr(copy, "copy", child_copy)
    assert dispatcher.main(w.argv) == 78
    capsys.readouterr()
    assert len(w.merges) == 1 and len(w.verifies) == 1
    with pytest.raises(
        ValueError, match="^finite callbacks require the exact CLI-constructed dispatcher backend$"
    ):
        _REAL_STAGED_PIPELINE(captured["args"], ["owner/repo#12"], captured["backend"], None)
    assert len(w.merges) == 1 and len(w.verifies) == 1 and w.events["provider"] == 0
