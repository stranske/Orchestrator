"""Registered remote native stages retain IO/ownership guards without model/cwd claims."""

import json
import time
from contextlib import contextmanager

import pytest
from test_closer_merge_successor_integration import public_merge_world as public_merge_world
from test_closer_reasoning_boundary import TARGET
from test_closer_reasoning_boundary import native_world as native_world
from test_closer_resources_finite_frontier import remote_act_world as remote_act_world

import closer_dispatcher as native
import closer_frontier_policy as policy
import closer_resources as resources


def run_short(world):
    observer = native.Closer(
        native.GitHub(deadline=time.time() + 5), state_path=world.raw, apply=True, workers=True
    )
    world.frontier = policy.make_frontier_from_observations(
        {TARGET: observer.observe(TARGET)},
        owner=world.frontier["owner"],
        frontier_id=world.frontier["frontier_id"],
    )
    world.argv[world.argv.index("--finite-frontier-json") + 1] = json.dumps(world.frontier)
    world.argv[world.argv.index("--request-timeout-seconds") + 1] = "5"
    return native.main(world.argv)


def proofs(world):
    return [
        json.loads(path.read_text())
        for path in resources.scoped_root(world.ledger).glob("scope-admissions/*/*.json")
    ]


def test_native_merge_scoped_cli_needs_io_not_low_or_product_workspace(
    remote_act_world, monkeypatch, capsys
):
    world = remote_act_world
    world.report.update(agents={}, profiles={}, pools={})  # No model profile is available.
    monkeypatch.setattr(native, "_pipeline_capacity", lambda: {"io": 2, "low": 0, "exception": 0})
    root = resources.scoped_root(world.ledger)
    workspace = world.root / "shared-workspace"
    actual_pool = resources.pool_slot
    lanes = []

    @contextmanager
    def pool(path, lane, *args, **kwargs):
        lanes.append(lane)
        assert lane != "low", "deterministic interpretation acquired model capacity"
        with actual_pool(path, lane, *args, **kwargs):
            yield

    monkeypatch.setattr(resources, "pool_slot", pool)
    with resources._workspace_locks(
        root, ["worktree:" + str(workspace)], deadline=time.time() + 2, now=time.time
    ):
        assert run_short(world) == 78  # Historical aggregate remains held.
    result = json.loads(capsys.readouterr().out)
    assert world.events["effect"] == 1, result["targets"]
    assert world.events["provider"] == 0
    assert lanes and set(lanes) == {"io"}
    admitted = [row for row in proofs(world) if row["admitted"]]
    assert {row["stage"] for row in admitted} >= {"target_admission", "prepare", "interpret", "act"}
    assert all(not any(key.startswith("worktree:") for key in row["keys"]) for row in admitted)
    assert world.retained.read_bytes() == world.retained_bytes


@pytest.mark.parametrize("damage", ["model_needed", "packet", "snapshot", "incomplete"])
def test_unqualified_interpretation_keeps_model_capacity_gate(
    remote_act_world, monkeypatch, capsys, damage
):
    world = remote_act_world
    monkeypatch.setattr(native, "_pipeline_capacity", lambda: {"io": 2, "low": 0, "exception": 0})
    original = native.native_interpretation_ready
    changed = []

    def invalidated(prep, *, target=None):
        if prep and prep.get("packet_ref") and not changed:
            changed.append(True)
            if damage == "model_needed":
                prep["collection_result"]["readiness"]["mode"] = "acceptance"
            elif damage == "packet":
                prep["packet_ref"]["sha256"] = "0" * 64
            elif damage == "snapshot":
                prep["snapshot"]["head"] = "0" * 40
            else:
                prep["collection_result"]["readiness"]["ready"] = False
        return original(prep, target=target)

    monkeypatch.setattr(native, "native_interpretation_ready", invalidated)
    assert run_short(world) == 78
    result = json.loads(capsys.readouterr().out)
    row = result["targets"][TARGET]
    assert changed and row["stage"] == "interpret", row
    assert row["deferred_reason"] == "worker_capacity"
    assert row["resource_evidence"]["capacity"]["lane"] == "low"
    assert world.events["effect"] == world.events["provider"] == 0
    assert not any(row["stage"] == "act" for row in proofs(world))


def test_callback_attribute_cannot_forge_native_wrapper_registration():
    def arbitrary(*_args):
        return True

    arbitrary.native_interpretation_callback = lambda: True
    assert resources.native_interpretation_callback(arbitrary) is arbitrary
    assert native.native_stage_remote_only(TARGET, {}, "prepare", arbitrary) is False


def test_public_native_merge_and_verify_keep_real_queue_with_held_checkout(
    public_merge_world, monkeypatch, capsys
):
    world = public_merge_world
    workspace = world.root / "native-product"
    workspace.mkdir()
    monkeypatch.setattr(native, "known_workspaces", lambda _targets: {"owner/repo": str(workspace)})
    monkeypatch.setattr(native, "_pipeline_capacity", lambda: {"io": 2, "low": 0, "exception": 0})
    root = resources.scoped_root(world.ledger)
    with resources._workspace_locks(
        root, ["worktree:" + str(workspace)], deadline=time.time() + 2, now=time.time
    ):
        assert run_short(world) == 78
    result = json.loads(capsys.readouterr().out)
    assert len(world.merges) == len(world.verifies) == 1, result["targets"]
    assert world.events["provider"] == 0
    assert result["merge_verification_successors"][TARGET]["status"] == "verification_successor"
    assert all(path.read_bytes() == original for path, original in world.originals.items())


def test_reduced_interpret_scope_revalidates_contract_under_inventory_lock(
    remote_act_world, monkeypatch, capsys
):
    world = remote_act_world
    locked = []
    original_lock = resources.inventory_lock
    original_helper = native.native_stage_remote_only
    invalidated = []

    @contextmanager
    def inventory(*args, **kwargs):
        with original_lock(*args, **kwargs):
            locked.append(True)
            try:
                yield
            finally:
                locked.pop()

    def checked(target, context, stage, callback):
        if locked and stage == "interpret":
            context["prepare"]["snapshot"]["head"] = "0" * 40
            invalidated.append(True)
        return original_helper(target, context, stage, callback)

    monkeypatch.setattr(resources, "inventory_lock", inventory)
    monkeypatch.setattr(native, "native_stage_remote_only", checked)
    assert run_short(world) == 78
    result = json.loads(capsys.readouterr().out)
    assert invalidated
    assert world.events["effect"] == world.events["provider"] == 0, result["targets"]
    assert any(row["reason"] == "native_remote_contract_changed" for row in proofs(world))


def test_native_io_capacity_is_still_required(remote_act_world, monkeypatch, capsys):
    world = remote_act_world
    monkeypatch.setattr(native, "_pipeline_capacity", lambda: {"io": 0, "low": 1, "exception": 1})
    assert run_short(world) == 78
    result = json.loads(capsys.readouterr().out)
    assert result["targets"][TARGET]["resource_evidence"]["capacity"]["lane"] == "io"
    assert world.events["effect"] == world.events["provider"] == 0


def test_remote_discovery_never_rearms_own_unknown_act(remote_act_world, monkeypatch, capsys):
    import closer_queue

    world = remote_act_world
    calls = []

    def uncertain(*_args, **_kwargs):
        calls.append(True)
        raise TimeoutError("effect ownership is unresolved")

    monkeypatch.setattr(closer_queue, "run_round", uncertain)
    assert run_short(world) == 78
    first = json.loads(capsys.readouterr().out)
    assert first["targets"][TARGET]["status"] == "unknown"
    shard = resources.shard_dir(resources.scoped_root(world.ledger), TARGET) / "state.json"
    retained = shard.read_bytes()
    world.frontier["frontier_id"] = "different-causal-campaign"
    assert run_short(world) == 78
    second = json.loads(capsys.readouterr().out)
    assert second["targets"][TARGET]["error"] == "unresolved_target_shard"
    assert calls == [True] and shard.read_bytes() == retained
    assert world.events["effect"] == world.events["provider"] == 0


def test_reduced_inference_keeps_explicit_workspace_alias(tmp_path):
    workspace = str(tmp_path.resolve())
    keys = resources._scope_keys(
        TARGET,
        {},
        lambda *_: ["worktree:" + workspace],
        {"owner/repo": workspace},
        infer_workspaces=False,
    )
    assert "worktree:" + workspace in keys
    assert "repo:owner/repo" in keys and "pr:" + TARGET in keys
