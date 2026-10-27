"""Bounded causal regressions for the local #963 review repair."""

import pytest

import closer_frontier_policy as policy
import closer_pipeline as pipeline
import closer_resources as resources
import closer_stage_continuation as continuation
from test_closer_hold_escalation import world, run_preflight
import closer_hold_escalation as escalation


def test_acknowledged_receiver_survives_resolved_hold_and_pruned_payload(tmp_path, monkeypatch, capsys):
    c, m, p, route, now = world(tmp_path, monkeypatch)
    escalation.service(c, m, p, now=now)
    monkeypatch.setattr(escalation.time, "time", lambda: now + 1)
    run_preflight(route, capsys)
    row = next(iter(c.read()["hold_escalations"].values()))
    assert row["status"] == "acknowledged"
    c.update(lambda doc: doc["hold_followups"]["hold"].update(status="resolved"))
    from pathlib import Path
    Path(row["payload_ref"]).unlink()
    before = c.path.read_bytes()
    run_preflight(route, capsys)
    assert c.path.read_bytes() == before


@pytest.mark.parametrize("missing", [False, True])
def test_assignment_without_outstanding_delivery_needs_no_route(tmp_path, monkeypatch, missing):
    c, m, p, route, now = world(tmp_path, monkeypatch)
    if missing:
        c.path.unlink()
    monkeypatch.setattr(escalation, "route", lambda *_: pytest.fail("empty work needs no route"))
    assignment = {"targets": ["untouched"], "nested": {"value": 1}}
    result = escalation.preserve_assignment(p, "unrelated-controller", assignment)
    assert result == assignment and result is not assignment


def test_retained_receipt_replay_at_cap_keeps_bound_and_conflict_checks(tmp_path, monkeypatch):
    target = "offline/repo#1"
    frontier = policy.make_frontier_from_observations(
        {target: {"target": target, "revision": "r", "eligible": True, "action": "merge", "operation_binding": {"pr_target": target, "source_targets": []}}},
        owner="owner", frontier_id="child",
    )
    root = tmp_path / "resources"
    root.mkdir()
    monkeypatch.setattr(resources, "scoped_root", lambda _: root)
    receipt = {"round_id": "existing", "finite_frontier": frontier, "assignment": [target], "run_ids": {target: "run"}}
    monkeypatch.setattr(resources, "read_round_receipt", lambda _: (receipt, "a" * 64))
    native = {"round_id": "existing", "receipt_path": str(root / "rounds" / "existing.json"), "receipt_sha256": "a" * 64}
    path = tmp_path / "pipeline.json"
    state = pipeline._empty()
    state["stage_successors"] = {"child": {"finite_frontier": frontier, "receipt_refs": []}}
    pipeline._save(path, state)
    retained = continuation.retain_receipt(path, root, target=target, frontier=frontier, native=native)
    state = pipeline._load(path)
    state["stage_successors"]["child"]["receipt_refs"] += [{"round_id": str(i)} for i in range(31)]
    pipeline._save(path, state)
    before = path.read_bytes()
    assert continuation.retain_receipt(path, root, target=target, frontier=frontier, native=native) == retained
    assert path.read_bytes() == before
    receipt["round_id"] = native["round_id"] = "new"
    native["receipt_path"] = str(root / "rounds" / "new.json")
    with pytest.raises(ValueError, match="retention bound"):
        continuation.retain_receipt(path, root, target=target, frontier=frontier, native=native)
    assert path.read_bytes() == before
    receipt["round_id"] = native["round_id"] = "existing"
    native["receipt_path"] = str(root / "rounds" / "existing.json")
    receipt["run_ids"] = {target: "different-run"}
    with pytest.raises(ValueError, match="identity conflicts"):
        continuation.retain_receipt(path, root, target=target, frontier=frontier, native=native)
    assert path.read_bytes() == before
