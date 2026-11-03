from __future__ import annotations

import fcntl
import json

import pytest

import closer_frontier_policy as policy
import closer_pipeline as pipeline
import closer_queue as queue
import closer_stage_continuation as continuation


@pytest.fixture
def case(tmp_path, monkeypatch):
    target = "acme/repo#8"
    binding = {"pr_target": target, "source_targets": []}
    old = policy.make_frontier_from_observations(
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
    observation = {
        "target": target,
        "revision": "r2",
        "action": "verify",
        "eligible": True,
        "operation_binding": binding,
        "observed_at": 20,
    }
    row = {
        "target": target,
        "revision": "r2",
        "action": "verify",
        "eligible": True,
        "status": "observed",
        "operation_id": "op-1",
        "operation_status": "completed",
    }
    receipt = {"accounting": [row], "events": [{"kind": "action_advanced"}], "stop_valid": True}
    progress = continuation._progress(receipt, row)
    operation = {
        "id": "op-1",
        "status": "completed",
        "revision": "r1",
        "action": "merge",
        "binding": binding,
        "finite_frontier": old,
        "completed_at": 10,
        "result": {"merge_sha": "merged-sha"},
    }
    item = {
        "status": "held",
        "stage": "act",
        "stage_status": "completed",
        "deferred_next_trigger": "new_frontier_required",
        "progress_key": progress,
        "results": {
            "act": {
                "status": "completed",
                "target_complete": False,
                "queue_receipt": receipt,
                "progress_key": progress,
            }
        },
    }
    path, action, canonical = (
        tmp_path / "pipeline.json",
        tmp_path / "action.json",
        tmp_path / "assignment.json",
    )
    document = pipeline._empty()
    document["frontiers"] = {
        old["frontier_id"]: {
            "schema": "closer-pipeline-frontier/v2",
            "finite_frontier": old,
            "run_id": "original-run",
            "items": {target: item},
        }
    }
    pipeline._save(path, document)
    qstate = {
        "schema": queue.SCHEMA,
        "items": {},
        "round": {},
        "frontiers": {
            old["frontier_id"]: {
                "policy": old,
                "items": {target: {"operation": operation, "operation_history": []}},
                "round": {"finite_frontier": old},
            }
        },
    }
    action.write_text(json.dumps(qstate))
    canonical_row = {"target": target, "lane": "closer", "fingerprint": "frozen"}
    canonical.write_text(json.dumps({"targets": [canonical_row]}))
    monkeypatch.setattr(
        queue, "validate_stop", lambda *_a, **_k: {"valid": True, "stop_valid": True}
    )
    args = dict(
        predecessor=old,
        target=target,
        observation=observation,
        canonical_path=canonical,
        canonical_row=canonical_row,
        runtime_binding={"generation": "qualified-current"},
        admission=lambda: {"paused": False, "dependency": None, "live_owner": False},
        now=lambda: 30,
    )
    return path, action, args, qstate, document


def invoke(case):
    path, action, args, *_ = case
    return continuation.create_successor(path, action, **args)


def test_causal_successor_restarts_same_frontier_without_copying_stage_results(case):
    path, _, args, _, original = case
    first = invoke(case)
    second = invoke(case)
    state = pipeline._load(path)
    successor = state["stage_successors"][first["finite_frontier"]["frontier_id"]]
    assert first["finite_frontier"] == second["finite_frontier"]
    assert not first["reused"] and second["reused"]
    assert first["execution_targets"] == [args["target"]]
    assert "items" not in successor
    assert set(state["frontiers"]) == {"predecessor"}
    assert successor["stage_predecessor"]["operation_id"] == "op-1"
    assert successor["stage_predecessor"]["predecessor_generation"] is None
    assert not first["acceptance_authority"]
    assert state["frontiers"]["predecessor"] == original["frontiers"]["predecessor"]


@pytest.mark.parametrize(
    "change",
    [
        "unknown",
        "missing_receipt",
        "bad_progress",
        "not_completed",
        "no_effect",
        "uncertain",
        "stale_observation",
    ],
)
def test_unproven_or_ineffective_predecessor_never_creates_successor(case, change):
    path, action, args, qstate, document = case
    before = path.read_bytes()
    item = document["frontiers"]["predecessor"]["items"][args["target"]]
    op = qstate["frontiers"]["predecessor"]["items"][args["target"]]["operation"]
    if change == "unknown":
        item["status"] = "unknown"
    elif change == "missing_receipt":
        item["results"]["act"].pop("queue_receipt")
    elif change == "bad_progress":
        item["progress_key"] = "invented"
    elif change == "not_completed":
        op["status"] = "confirmed_not_started"
    elif change == "no_effect":
        op["result"]["no_effect"] = True
    elif change == "uncertain":
        op["result"]["outcome_uncertain"] = True
    elif change == "stale_observation":
        args["observation"]["observed_at"] = 5
    pipeline._save(path, document)
    action.write_text(json.dumps(qstate))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        invoke(case)
    assert path.read_bytes() == before


@pytest.mark.parametrize("gate", ["paused", "dependency", "live_owner"])
def test_normal_target_gates_remain_authoritative(case, gate):
    args = case[2]
    gates = {"paused": False, "dependency": None, "live_owner": False}
    gates[gate] = "blocked" if gate == "dependency" else True
    args["admission"] = lambda: gates
    with pytest.raises(ValueError, match="ordinary"):
        invoke(case)


def test_live_original_runlock_and_canonical_drift_refuse(case):
    path, _, args, *_ = case
    with path.with_name(path.name + ".runlock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="still live"):
            invoke(case)
    args["canonical_path"].write_text(
        json.dumps({"targets": [{"target": args["target"], "fingerprint": "new"}]})
    )
    with pytest.raises(ValueError, match="canonical"):
        invoke(case)


def test_supplied_valid_flag_does_not_replace_native_queue_validation(case, monkeypatch):
    monkeypatch.setattr(
        queue, "validate_stop", lambda *_a, **_k: {"valid": False, "stop_valid": False}
    )
    with pytest.raises(ValueError, match="authoritative"):
        invoke(case)


def test_changed_action_required_even_after_real_completion(case):
    args = case[2]
    args["observation"].update(action="merge", revision="r1")
    with pytest.raises(ValueError, match="unchanged action"):
        invoke(case)


def test_same_action_projection_change_is_not_a_new_stage(case):
    case[2]["observation"].update(action="merge", revision="producer-only-new-revision")
    with pytest.raises(ValueError, match="unchanged action"):
        invoke(case)


def test_queue_mutation_after_public_validation_fails_compare_and_swap(case, monkeypatch):
    path, action, _, qstate, _ = case
    before = path.read_bytes()

    def validate(*_args, **_kwargs):
        qstate["frontiers"]["predecessor"]["items"][case[2]["target"]]["operation"][
            "status"
        ] = "uncertain"
        action.write_text(json.dumps(qstate))
        return {"valid": True, "stop_valid": True}

    monkeypatch.setattr(queue, "validate_stop", validate)
    with pytest.raises(ValueError, match="queue changed"):
        invoke(case)
    assert path.read_bytes() == before


def test_restart_projection_drift_reuses_exact_successor_not_new_namespace(case):
    path, _, args, *_ = case
    first = invoke(case)
    args["observation"]["revision"] = "later-producer-revision"
    second = invoke(case)
    assert second["reused"] and second["fresh_binding_drift"]
    assert second["finite_frontier"] == first["finite_frontier"]
    assert len(pipeline._load(path)["frontiers"]) == 1
    assert len(pipeline._load(path)["stage_successors"]) == 1


def test_unknown_runtime_provenance_is_not_a_new_admission_gate(case):
    case[2]["runtime_binding"] = None
    result = invoke(case)
    assert result["predecessor"]["current_runtime_binding"] is None


def test_normal_pipeline_initializes_successor_without_stage_injection(case):
    path, _, args, *_ = case
    result = invoke(case)
    calls = []
    receipt = pipeline._run_pipeline_finite(
        path,
        [args["target"]],
        finite_frontier=result["finite_frontier"],
        frontier_target=args["target"],
        prepare=lambda *_a: calls.append("prepare") or {"revision": "r2"},
        interpret=lambda *_a: calls.append("interpret") or {"substantive_needs": []},
        exception=lambda *_a: {"route": "normal"},
        act=lambda *_a: calls.append("act") or {"status": "completed", "target_complete": True},
        capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
        now=lambda: 30,
    )
    assert calls == ["prepare", "interpret", "act"]
    assert receipt["targets"][args["target"]]["status"] == "completed"
    assert pipeline._load(path)["frontiers"]["predecessor"] == case[4]["frontiers"]["predecessor"]


def test_successor_metadata_does_not_publish_census_malformed_empty_frontier(case, tmp_path):
    import closer_quarantine
    import closer_resources

    path, _, args, *_ = case
    invoke(case)
    shared = tmp_path / "scope.pipeline.json"
    pipeline._save(shared, pipeline._empty())
    root = closer_resources.scoped_root(shared)
    directory = closer_resources.shard_dir(root, args["target"])
    directory.mkdir(parents=True)
    (root / "contract.json").write_text(
        json.dumps(
            {
                "schema": closer_resources.CONTRACT_SCHEMA,
                "layout": closer_resources.LAYOUT,
                "pipeline_schema": pipeline.SCHEMA,
                "coordination_gates": 1,
            }
        )
    )
    (directory / "lease.json").write_text(json.dumps({"target": args["target"].lower()}))
    (directory / "state.json").write_bytes(path.read_bytes())
    snapshot = closer_quarantine.scope_snapshot(shared)
    assert not snapshot["global_reasons"]
    assert not any(row["reason"] == "malformed_anchored_shard" for row in snapshot["items"])


def test_multitarget_predecessor_produces_singleton_child_with_frozen_cohort_provenance(case):
    path, action, args, qstate, document = case
    target, sibling = args["target"], "acme/repo#9"
    original = args["predecessor"]
    bindings = {
        **original["bindings"],
        sibling: {
            "revision": "sibling-r1",
            "action_id": policy.action_identity(
                sibling, "sibling-r1", "review", {"pr_target": sibling}
            ),
        },
    }
    cohort = policy.make_frontier(
        [target, sibling],
        owner=original["owner"],
        frontier_id=original["frontier_id"],
        bindings=bindings,
    )
    args["predecessor"] = cohort
    document["frontiers"]["predecessor"]["finite_frontier"] = cohort
    bucket = qstate["frontiers"]["predecessor"]
    bucket["policy"] = cohort
    bucket["round"]["finite_frontier"] = cohort
    bucket["items"][target]["operation"]["finite_frontier"] = cohort
    pipeline._save(path, document)
    action.write_text(json.dumps(qstate))
    result = invoke(case)
    child = policy.validate_frontier(result["finite_frontier"], targets=[target])
    assert child["targets"] == result["execution_targets"] == [target]
    assert set(child["bindings"]) == {target}
    assert result["predecessor"]["frozen_cohort"] == {
        "targets": [target, sibling],
        "targets_sha256": cohort["targets_sha256"],
        "owner": cohort["owner"],
        "bindings_sha256": cohort["bindings_sha256"],
    }
    calls = []
    pipeline._run_pipeline_finite(
        path,
        [target],
        finite_frontier=child,
        frontier_target=target,
        prepare=lambda name, *_a: calls.append(name) or {"revision": "r2"},
        interpret=lambda *_a: {"substantive_needs": []},
        exception=lambda *_a: {"route": "normal"},
        act=lambda *_a: {"status": "completed", "target_complete": True},
        capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
        now=lambda: 30,
    )
    assert calls == [target] and sibling not in calls
    assert pipeline._load(path)["frontiers"]["predecessor"] == document["frontiers"]["predecessor"]
    assert invoke(case)["finite_frontier"] == child


def test_successor_never_widens_original_cohort(case):
    case[2]["target"] = "acme/other#99"
    with pytest.raises(ValueError, match="expand its frozen cohort"):
        invoke(case)
