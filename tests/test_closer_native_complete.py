"""Closed issue observation needs IO, without model or acceptance authority."""

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

_REAL_QUEUE = queue.run_round
_REAL_CAPACITY = native._pipeline_capacity


def complete_world(world, monkeypatch):
    world.ad.unlink()
    world.snapshot.clear()
    world.snapshot.update(
        target=TARGET,
        kind="issue",
        state="CLOSED",
        body="Original request",
        sources=[
            {
                "target": TARGET,
                "kind": "issue",
                "state": "closed",
                "body": "Original request",
                "labels": [],
            }
        ],
        delivery_links={
            "complete": True,
            "status": "missing",
            "candidates": [],
            "acceptance_authority": False,
        },
        observed_at=time.time(),
    )
    world.snapshot["revision"] = native.digest(world.snapshot)
    world.report.update(agents={}, profiles={}, pools={})
    monkeypatch.setattr(native, "_pipeline_capacity", _REAL_CAPACITY)
    monkeypatch.setattr(queue, "run_round", _REAL_QUEUE)
    import dispatcher

    def no_provider(*_args, **_kwargs):
        pytest.fail("closed issue must not allocate a provider")

    monkeypatch.setattr(dispatcher, "offload", no_provider)
    observer = native.Closer(
        native.GitHub(deadline=time.time() + 10), state_path=world.raw, apply=True, workers=True
    )
    observed = observer.observe(TARGET)
    assert observed["action"] == "complete" and observed["eligible"] is False
    world.frontier = policy.make_frontier(
        [TARGET],
        owner=world.frontier["owner"],
        frontier_id=world.frontier["frontier_id"],
        bindings={
            TARGET: {
                "revision": observed["revision"],
                "action_id": policy.action_identity(
                    TARGET, observed["revision"], "complete", observed["operation_binding"]
                ),
            }
        },
    )
    world.argv[world.argv.index("--finite-frontier-json") + 1] = json.dumps(world.frontier)
    return world


def run_complete(world, capsys):
    native.main(world.argv)
    return json.loads(capsys.readouterr().out)["targets"][TARGET]


def test_actual_registered_closed_issue_uses_io_without_model(native_world, monkeypatch, capsys):
    world = complete_world(native_world, monkeypatch)
    assert _REAL_CAPACITY()["low"] == 0
    row = run_complete(world, capsys)
    assert row["status"] == "completed" and row["stage"] == "act", row
    assert row["results"]["interpret"]["classification"] == "deterministic_complete"
    assert row["results"]["prepare"]["drift_audit_candidate"] is True
    assert not row["results"]["interpret"].get("drift_review_complete")
    assert world.events["provider"] == world.events["effect"] == 0


@pytest.mark.parametrize(
    "damage",
    [
        "snapshot_hash",
        "packet_hash",
        "missing_snapshot",
        "target",
        "revision",
        "producer",
        "readiness",
        "cached",
        "generic_complete",
        "unknown",
        "recovery",
        "findings",
        "old",
        "future",
        "nonfinite",
        "head",
        "open",
        "pr",
        "pending_ci",
        "delivery",
        "security",
        "source",
        "disposition",
    ],
)
def test_unbound_closed_issue_stays_on_existing_capacity_route(
    native_world, monkeypatch, capsys, damage
):
    world = complete_world(native_world, monkeypatch)
    helper = native._native_complete_snapshot
    checked = []

    def damaged(prep, target):
        prep = copy.deepcopy(prep)
        checked.append(True)
        if damage in {"snapshot_hash", "packet_hash"}:
            prep[damage.removesuffix("_hash") + "_ref"]["sha256"] = "0" * 64
        elif damage == "missing_snapshot":
            prep.pop("snapshot_ref")
        elif damage in {"target", "revision"}:
            prep["snapshot"][damage] = "owner/repo#999" if damage == "target" else "wrong"
        elif damage == "producer":
            prep["producer_sha256"] = "0" * 64
        elif damage == "readiness":
            prep["collection_result"]["readiness"]["gaps"] = ["source:missing"]
        elif damage == "cached":
            prep["remote_fresh"] = False
        elif damage == "generic_complete":
            prep["proposal"]["reason"] = "generic complete"
        elif damage == "unknown":
            prep["provider_outcome_uncertain"] = {"run_id": "original"}
        elif damage == "recovery":
            prep["recovery_barrier"] = {"run_id": "original"}
        elif damage == "findings":
            prep["findings"] = [{"id": "unresolved"}]
        elif damage == "disposition":
            # A genuine prior unresolved disposition is retained in a valid packet.
            packet_ref = prep["packet_ref"]
            packet = json.loads(Path(packet_ref["path"]).read_bytes())
            packet["sections"]["prior_dispositions"]["content"] = {"findings": ["unresolved"]}
            raw = json.dumps(packet).encode()
            path = Path(packet_ref["path"]).with_name("bad-disposition.json")
            path.write_bytes(raw)
            prep["packet_ref"] = {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}
        else:
            ref = prep["snapshot_ref"]
            snapshot = json.loads(Path(ref["path"]).read_bytes())
            if damage in {"old", "future", "nonfinite"}:
                snapshot["observed_at"] = {
                    "old": time.time() - 121,
                    "future": time.time() + 100,
                    "nonfinite": float("nan"),
                }[damage]
            elif damage == "head":
                snapshot["head"] = "a" * 40
            elif damage == "open":
                snapshot["state"] = "OPEN"
            elif damage == "pr":
                snapshot["kind"] = "pr"
            elif damage == "pending_ci":
                snapshot["pending_check_run_ids"] = [123]
            elif damage == "delivery":
                snapshot["delivery_links"]["complete"] = False
            elif damage == "security":
                snapshot["sources"][0]["labels"] = ["security"]
            else:
                snapshot["sources"][0]["target"] = "owner/repo#999"
            raw = json.dumps(snapshot).encode()
            path = Path(ref["path"]).with_name("damaged-" + damage + ".json")
            path.write_bytes(raw)
            prep["snapshot_ref"] = {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}
        return helper(prep, target)

    monkeypatch.setattr(native, "_native_complete_snapshot", damaged)
    row = run_complete(world, capsys)
    assert checked and row["stage"] == "interpret", row
    assert row["status"] == "capacity_blocked" and row["deferred_reason"] == "worker_capacity"
    assert row["resource_evidence"]["capacity"]["lane"] == "low"
    assert world.events["provider"] == world.events["effect"] == 0


def test_arbitrary_callback_cannot_claim_closed_issue_io():
    assert native.native_stage_remote_only(TARGET, {}, "interpret", lambda *_a: True) is False
