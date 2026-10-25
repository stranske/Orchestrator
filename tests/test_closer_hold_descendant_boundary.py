"""Actual native signatures cover the leaf before durable hold release authority."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from test_closer_ci_followup import public_ci_world as public_ci_world
from test_closer_native_capacity import capacity_host as capacity_host
from test_closer_native_descendants import native_descendant_world as native_descendant_world
from test_closer_native_observer import host as host

import closer_campaign as campaign
import closer_hold_followup as holds


def captured(world):
    _w, cfg, _ref, obj, *_ = world
    doc = holds.capture(obj)
    rows = list(doc["hold_followups"].values())
    assert len(rows) == 1 and rows[0]["source"].get("descendant_coverage")
    manifest = campaign.load_manifest(cfg["runtime_manifest"])
    return obj, doc, rows[0], manifest


def test_actual_known_native_boundary_signs_exact_active_descendant(native_descendant_world):
    obj, doc, row, manifest = captured(native_descendant_world)
    boundary = holds._native_terminal_boundary(obj, doc, manifest, row)
    assert boundary["record"]["descendant_coverage_sha256"] == holds._digest(
        row["source"]["descendant_coverage"]
    )
    holds._validate_terminal_boundary(obj, doc, manifest, row, boundary)
    assert row["frontier"] != doc["worker_config"]["finite_frontier"]


def test_original_root_proof_cannot_cover_arbitrary_child_without_leaf_receipt(
    native_descendant_world,
):
    obj, doc, row, manifest = captured(native_descendant_world)
    row = copy.deepcopy(row)
    row["source"].pop("descendant_coverage")
    with pytest.raises(ValueError, match="nonroot hold lacks"):
        holds._native_terminal_boundary(obj, doc, manifest, row)


@pytest.mark.parametrize("field", ["result", "bundle", "receipt", "coverage"])
def test_signed_original_descendant_replay_refuses_changed_bytes(native_descendant_world, field):
    obj, doc, row, manifest = captured(native_descendant_world)
    boundary = holds._native_terminal_boundary(obj, doc, manifest, row)
    coverage = row["source"]["descendant_coverage"]
    worker = coverage["source_worker"]
    if field == "coverage":
        row["source"]["descendant_coverage"]["chain_sha256"] = "f" * 64
    else:
        path = Path(
            {
                "result": worker["result_path"],
                "bundle": worker["bundle_ref"]["path"],
                "receipt": coverage["receipt_ref"]["receipt_path"],
            }[field]
        )
        path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError):
        holds._validate_terminal_boundary(obj, doc, manifest, row, boundary)


def test_actual_signed_leaf_wait_remains_owned_on_restart_without_terminal_bridge(
    native_descendant_world,
):
    w, cfg, _ref, obj, *_ = native_descendant_world
    obj, doc, row, manifest = captured(native_descendant_world)
    action_path = (
        Path(row["source"]["state_path"]).parent
        / "state.actions"
        / (holds._digest(row["target"]) + ".json")
    )
    before_queue = action_path.read_bytes()
    before_source = Path(row["source"]["state_path"]).read_bytes()
    initial = {
        key: row[key]
        for key in ("hold_id", "created_at", "deadline", "attempt_budget", "run_id", "frontier")
    }
    updated, count = holds.consume(obj, manifest, cfg["runtime_manifest"])
    observed = updated["hold_followups"][row["hold_id"]]
    assert len(count) == 1 and observed["status"] == "waiting"
    assert (
        observed["last_result"]["reason"]
        == "original sent correlation lacks a current authenticated terminal event/run/attempt"
    )
    assert observed["next_check_at"] > observed["created_at"]
    assert all(observed[key] == value for key, value in initial.items())
    restarted = campaign.Campaign(obj.path)
    resumed, count = holds.consume(restarted, manifest, cfg["runtime_manifest"])
    assert count == []
    retained = resumed["hold_followups"][row["hold_id"]]
    assert retained == observed
    assert Path(row["source"]["state_path"]).read_bytes() == before_source
    assert action_path.read_bytes() == before_queue
    assert len(w.merges) == len(w.verifies) == 1 and w.events["provider"] == 0
