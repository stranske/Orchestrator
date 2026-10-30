"""The ownership observer gets a fresh timestamp at its own boundary."""

import hashlib
import sys
from types import SimpleNamespace

import pytest

import closer_correction_owner as owner
import closer_correction_ownership as ownership


def test_native_ownership_rejects_a_stale_clock(monkeypatch):
    monkeypatch.setattr(ownership.time, "time", lambda: 102.01)
    with pytest.raises(ownership.OwnershipUnavailable, match="clock is not current"):
        ownership.collect_ownership(github=object(), seed={}, deadline=200, now=100.0)


def test_owner_refreshes_clock_before_native_ownership_observation(monkeypatch):
    values = iter([100.0, 100.0, *([102.01] * 8)])
    monkeypatch.setattr(owner.time, "time", lambda: next(values))
    monkeypatch.setattr(
        owner,
        "_current",
        lambda *args, **kwargs: (
            {"stage_results": {"interpret": {"work_key": "low"}}},
            {"owner": "o"},
            lambda: None,
        ),
    )
    snapshot = {"target": "stranske/Orchestrator#984", "revision": "r", "head": "h", "base": "b"}
    packet, snapshot_raw, packet_raw = {}, b"snapshot", b"packet"
    monkeypatch.setattr(
        owner,
        "_physical_json",
        lambda path: (snapshot, snapshot_raw) if path == "snap" else (packet, packet_raw),
    )
    monkeypatch.setattr(
        owner.bridge,
        "collect_original_inputs",
        lambda *args: {
            "seed_bundle": {},
            "source_body": {},
            "raw_findings": {},
            "retained_packet_ref": {},
        },
    )
    monkeypatch.setattr(owner, "_stage", lambda *args, **kwargs: ({}, {}))
    seed = {
        "target": {"repository": "stranske/Orchestrator", "pr": "984"},
        "source_issue": {"number": "983"},
        "original_verifier": {},
        "finding": {},
        "scope": {},
    }
    monkeypatch.setattr(
        owner, "_selected_defect", lambda **kwargs: (seed, [], {"request_id": "request"})
    )
    monkeypatch.setattr(owner.contract, "digest", lambda value: "digest")
    monkeypatch.setattr(owner.contract, "build_request", lambda **kwargs: None)
    import closer_correction_handoff as handoff

    monkeypatch.setattr(handoff, "validate_request", lambda value: None)
    monkeypatch.setattr(handoff, "_ownership", lambda *args: None)
    monkeypatch.setitem(
        sys.modules,
        "closer_execution_contracts",
        SimpleNamespace(load_retained_packet=lambda ref: (packet, object())),
    )
    seen = {}

    class Observed:
        def document(self):
            return {
                "ownership": {"seed_effect_key": "a", "source_scope_digest": "b"},
                "incumbent": {},
            }

    def collect(**kwargs):
        seen.update(now=kwargs["now"], current=owner.time.time())
        return Observed()

    monkeypatch.setattr(owner.ownership, "collect_ownership", collect)
    result = owner.build_owner_request(
        context={
            "interpret": {"actual": {"kind": "source_native_conservative_inventory"}},
            "work_key": "strong",
            "deadline": 200,
            "pipeline_run_id": "p",
            "finite_frontier": {"frontier_id": "f"},
        },
        preparation={
            "packet_ref": {"path": "packet", "sha256": hashlib.sha256(packet_raw).hexdigest()},
            "snapshot_ref": {"path": "snap", "sha256": hashlib.sha256(snapshot_raw).hexdigest()},
            "revision": "r",
            "snapshot": snapshot,
        },
        stage_result={
            "actual": {
                "_closer_controlled_proof": {
                    "expected": {
                        "basis": {
                            "target": snapshot["target"],
                            "revision": "r",
                            "head": "h",
                            "base": "b",
                            "packet_sha256": hashlib.sha256(packet_raw).hexdigest(),
                            "request_id": "request",
                        }
                    }
                }
            }
        },
        github=object(),
    )
    assert result["status"] == "ready"
    assert seen == {"now": 102.01, "current": 102.01}
