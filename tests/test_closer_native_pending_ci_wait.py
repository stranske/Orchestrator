"""Exact pending CI reaches native IO/queue ownership without model qualification."""

import copy
import hashlib
import json
import time
from pathlib import Path

import pytest
from test_closer_reasoning_boundary import TARGET
from test_closer_reasoning_boundary import native_world as native_world

import closer_dispatcher as native
import closer_frontier_policy as policy
import closer_queue as queue
import closer_resources as resources

_REAL_QUEUE = queue.run_round
_REAL_CAPACITY = native._pipeline_capacity


def pending_world(world, monkeypatch):
    world.ad.unlink()
    world.snapshot.update(
        state="OPEN",
        merge=None,
        verifier=None,
        checks="PENDING",
        pending_check_run_ids=[123],
        pending_check_head=world.snapshot["head"],
    )
    world.snapshot["revision"] = native.digest(world.snapshot)
    world.report.update(agents={}, profiles={}, pools={})
    monkeypatch.setattr(native, "_pipeline_capacity", _REAL_CAPACITY)
    monkeypatch.setattr(queue, "run_round", _REAL_QUEUE)
    import dispatcher

    def no_provider(*_args, **_kwargs):
        pytest.fail("known pending CI must not allocate or invoke a provider")

    monkeypatch.setattr(dispatcher, "offload", no_provider)
    transport = native.gh_capacity.gh_run

    def github(args, **kwargs):
        if args[:4] == ["api", "--method", "GET", "repos/owner/repo/check-runs/123"]:
            return 0, {
                "id": 123,
                "head_sha": world.snapshot["head"],
                "status": "in_progress",
                "conclusion": None,
                "html_url": "https://github.com/owner/repo/runs/123",
            }
        return transport(args, **kwargs)

    monkeypatch.setattr(native.gh_capacity, "gh_run", github)
    observer = native.Closer(
        native.GitHub(deadline=time.time() + 10),
        state_path=world.raw,
        apply=True,
        workers=True,
    )
    observed = observer.observe(TARGET)
    assert observed["action"] == "wait" and observed["eligible"] is False
    world.frontier = policy.make_frontier(
        [TARGET],
        owner=world.frontier["owner"],
        frontier_id=world.frontier["frontier_id"],
        bindings={
            TARGET: {
                "revision": observed["revision"],
                "action_id": policy.action_identity(
                    TARGET, observed["revision"], "wait", observed["operation_binding"]
                ),
            }
        },
    )
    world.argv[world.argv.index("--finite-frontier-json") + 1] = json.dumps(world.frontier)
    world.argv[world.argv.index("--request-timeout-seconds") + 1] = "10"
    return world


def run_wait(world, capsys):
    native.main(world.argv)
    result = json.loads(capsys.readouterr().out)
    return result["targets"][TARGET]


def test_actual_registered_ci_wait_uses_io_and_original_no_effect_queue(
    native_world, monkeypatch, capsys
):
    world = pending_world(native_world, monkeypatch)
    assert _REAL_CAPACITY()["low"] == 0
    row = run_wait(world, capsys)
    assert row["stage"] == "act" and row["status"] == "held", row
    assert row["results"]["interpret"]["classification"] == "deterministic_wait"
    action = row["results"]["act"]
    assert action["status"] == "pending" and action["pending_kind"] == "ci"
    assert action["pending_evidence"]["check_run_id"] == 123
    shard = resources.shard_dir(resources.scoped_root(world.ledger), TARGET)
    action_path = shard / "state.actions" / f"{native.digest(TARGET)}.json"
    proof = queue.ci_wait_no_effect_proof(
        action_path,
        TARGET,
        frontier=world.frontier,
        receipt=action["queue_receipt"],
        evidence=action["pending_evidence"],
    )
    assert proof["check_run_id"] == 123 and proof["head"] == world.snapshot["head"]
    prep = row["results"]["prepare"]
    assert prep["drift_audit_candidate"] is True
    assert not row["results"]["interpret"].get("drift_review_complete")
    assert world.events["provider"] == world.events["effect"] == 0
    original = action_path.read_bytes()
    run_wait(world, capsys)
    assert action_path.read_bytes() == original


@pytest.mark.parametrize(
    "damage",
    [
        "ids",
        "check_head",
        "id_type",
        "duplicate_ids",
        "head",
        "unknown",
        "recovery",
        "cached",
        "generic_wait",
        "packet",
        "snapshot",
        "missing_snapshot",
        "target",
        "revision",
        "producer",
        "readiness",
    ],
)
def test_unbound_pending_ci_cannot_use_native_wait_lane(native_world, monkeypatch, capsys, damage):
    world = pending_world(native_world, monkeypatch)
    helper = native._native_pending_ci_snapshot
    checked = []

    def invalidated(prep, target):
        prep = copy.deepcopy(prep)
        if prep.get("snapshot_ref") and not checked:
            checked.append(True)
        if damage in {"ids", "check_head", "id_type", "duplicate_ids"}:
            ref = prep["snapshot_ref"]
            original = json.loads(Path(ref["path"]).read_bytes())
            if damage == "ids":
                original.pop("pending_check_run_ids")
            elif damage == "check_head":
                original["pending_check_head"] = "f" * 40
            elif damage == "id_type":
                original["pending_check_run_ids"] = [True]
            else:
                original["pending_check_run_ids"] = [123, 123]
            raw = json.dumps(original).encode()
            alternative = Path(ref["path"]).with_name("damaged-" + damage + ".json")
            alternative.write_bytes(raw)
            prep["snapshot_ref"] = {
                "path": str(alternative),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        elif damage == "head":
            prep["snapshot"]["head"] = "f" * 40
        elif damage == "unknown":
            prep["provider_outcome_uncertain"] = {"run_id": "original-unknown"}
        elif damage == "recovery":
            prep["recovery_barrier"] = {"run_id": "unknown-original"}
        elif damage == "missing_snapshot":
            prep.pop("snapshot_ref")
        elif damage in {"target", "revision"}:
            prep["snapshot"][damage] = "owner/repo#999" if damage == "target" else "different"
        elif damage == "producer":
            prep["producer_sha256"] = "0" * 64
        elif damage == "readiness":
            prep["collection_result"]["readiness"]["gaps"] = ["checks:unknown"]
        elif damage == "cached":
            prep["remote_fresh"] = False
        elif damage == "generic_wait":
            prep["proposal"]["reason"] = "unbound waiting"
        else:
            prep[damage + "_ref"]["sha256"] = "0" * 64
        return helper(prep, target)

    monkeypatch.setattr(native, "_native_pending_ci_snapshot", invalidated)
    row = run_wait(world, capsys)
    assert checked and row["stage"] == "interpret", row
    assert row["deferred_reason"] == "worker_capacity"
    assert row["resource_evidence"]["capacity"]["lane"] == "low"
    assert world.events["provider"] == world.events["effect"] == 0
