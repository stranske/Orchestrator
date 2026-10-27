"""Known retained/current identity contradictions collect before advisory allocation."""

import hashlib
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_closer_dispatcher import closer, no_live_state_or_provider
from test_closer_disposition import fixture
from test_closer_pre_model_readiness import prepared

import closer_dispatcher as adapter
import closer_evidence as evidence
import closer_pipeline


class AdvisoryRouteReached(RuntimeError):
    pass


def callback_probe(
    tmp_path, monkeypatch, *, damage=None, native=False, risk=None, prior=None, recovery=False
):
    no_live_state_or_provider.__wrapped__(monkeypatch, tmp_path)
    collect = evidence.collect_evidence

    def risk_collect(snapshot, *args, **kwargs):
        if risk:
            snapshot["verifier"]["category"] = risk
        return collect(snapshot, *args, **kwargs)

    with monkeypatch.context() as collecting:
        collecting.setattr(evidence, "collect_evidence", risk_collect)
        snap, packet, ref, _prep, _response = fixture(tmp_path, native=native)
    snap.update(state="MERGED" if native else "OPEN", active_threads=0, labels=[])
    if damage == "base":
        snap["base"] = "f" * 40
    elif damage == "review":
        snap["review_revision"] = "changed-review"
    elif damage == "head":
        snap["head"] = "f" * 40
    elif damage == "packet_producer":
        packet["producer_sha256"] = "f" * 64
        packet["semantic_cache_key"] = evidence._semantic_cache_key(packet)
        packet["manifest"]["packet_sha256"] = evidence._packet_hash(packet)
        path = Path(ref["path"]).with_name(packet["manifest"]["packet_sha256"] + ".json")
        path.write_bytes(evidence._bytes(packet))
        assert evidence.validate_packet(packet)
    prep = prepared(tmp_path, snap, packet, action="disposition" if native else "collect_evidence")
    if damage == "prep_producer":
        prep["producer_sha256"] = "0" * 64
    elif damage == "prep_revision":
        prep["revision"] = "changed-preparation"
    elif damage == "compact_target":
        prep["snapshot"]["target"] = "owner/repo#999"
    calls = []
    seen = {}
    backend = closer(tmp_path, snap)
    if prior is not None:
        request = {
            "basis": {"source": "owner/repo#2", "criterion": "Ship the report"},
            "question": "Does retained evidence support this criterion?",
            "evidence_refs": [],
        }
        if prior == "bad_future_basis":
            request["basis"] = {"source": "foreign/repo#999", "criterion": "invented"}
        elif prior == "metadata_future_ref":
            request["evidence_refs"] = [packet["sections"]["source"]["content"][0]["body_retained"]]
        else:
            request["evidence_refs"] = [
                {"path": str(tmp_path / "foreign/raw.json"), "sha256": "0" * 64, "bytes": 12}
            ]
        backend.dispositions[snap["target"]] = {"adjudication_request": request}

    def campaign(*args, **kwargs):
        calls.append("campaign")
        return {"allowed": True}

    def route(**kwargs):
        calls.append("route")
        raise AdvisoryRouteReached()

    def forbidden(*args, **kwargs):
        pytest.fail("identity probe must never execute provider/worker/action")

    backend.campaign_admission = campaign
    backend.worker = forbidden
    backend.run_fn = forbidden
    monkeypatch.setattr(adapter, "_pipeline_low_route", route)
    monkeypatch.setitem(sys.modules, "dispatcher", SimpleNamespace(offload=forbidden))

    def pipeline(_path, _targets, **callbacks):
        context = {
            "prepare": prep,
            "deadline": time.time() + 30,
            "work_key": "offline-identity-probe",
        }
        try:
            seen.update(callbacks["interpret"](snap["target"], context))
        except AdvisoryRouteReached:
            seen["advisory_route_reached"] = True
        return {"targets": {snap["target"]: seen}}

    monkeypatch.setattr(closer_pipeline, "run_pipeline", pipeline)
    args = SimpleNamespace(
        state=str(tmp_path / "state.json"), shadow=False, max_workers=1, evidence_recovery=recovery
    )
    adapter._run_staged_pipeline(args, [snap["target"]], backend, time.time() + 30)
    return snap, packet, prep, calls, seen


@pytest.mark.parametrize(
    "damage,gap",
    [
        ("base", "packet:base_mismatch"),
        ("review", "review_conversations:revision_binding_mismatch"),
        ("prep_producer", "packet:producer_mismatch"),
        ("packet_producer", "packet:producer_mismatch"),
        ("prep_revision", "packet:revision_mismatch"),
        ("head", "packet:head_mismatch"),
        ("compact_target", "packet:target_mismatch"),
    ],
)
@pytest.mark.parametrize("recovery", [False, True], ids=["ordinary", "finite-recovery"])
def test_known_identity_gap_collects_before_any_campaign_or_role_route(
    tmp_path, monkeypatch, damage, gap, recovery
):
    snap, packet, prep, calls, result = callback_probe(
        tmp_path, monkeypatch, damage=damage, recovery=recovery
    )
    assert calls == [], (damage, calls, result)
    assert result["classification"] == "evidence_pending" and result["status"] == "pending"
    assert result["pending_kind"] == "external_event" and result["authority"] is False
    pending = result["pending_evidence"]
    assert pending["target"] == snap["target"] and pending["revision"] == prep["revision"]
    assert gap in pending["readiness"]["gaps"]
    assert pending["packet_ref"] == prep["packet_ref"]
    assert result["substantive_needs"] == []
    if damage == "base":
        assert json.loads(Path(prep["snapshot_ref"]["path"]).read_bytes())["base"] != packet["base"]


@pytest.mark.parametrize(
    "kind",
    [
        "ordinary",
        "native",
        "security",
        "authority",
        "bad_future_basis",
        "metadata_future_ref",
        "foreign_future_ref",
    ],
)
@pytest.mark.parametrize("recovery", [False, True], ids=["ordinary", "finite-recovery"])
def test_same_identity_preserves_readonly_classification_applicability(
    tmp_path, monkeypatch, kind, recovery
):
    risk = {"security": "security", "authority": "authority_conflict"}.get(kind)
    prior = (
        kind if kind in {"bad_future_basis", "metadata_future_ref", "foreign_future_ref"} else None
    )
    snap, packet, prep, calls, result = callback_probe(
        tmp_path,
        monkeypatch,
        native=kind in {"native", "security", "authority"},
        risk=risk,
        prior=prior,
        recovery=recovery,
    )
    assert calls == ["campaign", "route"] and result == {"advisory_route_reached": True}
    assert prep["producer_sha256"] == packet["producer_sha256"]
    assert packet["base"] == snap["base"]
    assert packet["bindings"]["reviews"] == evidence._hash(snap["review_revision"])
    assert (
        packet["producer_sha256"]
        == hashlib.sha256(Path(evidence.__file__).read_bytes()).hexdigest()
    )
