"""Imported physical packet custody stays distinct from the current pipeline root."""

import copy
import hashlib
from pathlib import Path

import pytest
from test_closer_batch_interpretation import native_callbacks_for, native_delivery
from test_closer_disposition import TARGET

import closer_dispatcher as dispatcher
import closer_evidence as evidence


def imported(tmp_path, monkeypatch):
    delivery = native_delivery(tmp_path, monkeypatch)
    callbacks, context = native_callbacks_for(delivery, monkeypatch)
    packet, ref, proposal = delivery.caller.packet(delivery.current, tmp_path / "imported.evidence")
    delivery.proposal = proposal
    context = copy.deepcopy(context)
    context["prepare"].update(
        packet_ref=ref,
        producer_sha256=packet["producer_sha256"],
        semantic_cache_key=packet["semantic_cache_key"],
    )
    context["prepare"]["evidence_summary"]["section_digests"] = packet["section_digests"]
    return delivery, callbacks, context, packet, ref


def test_actual_imported_packet_restarts_from_its_original_raw_custody(tmp_path, monkeypatch):
    delivery, callbacks, context, packet, ref = imported(tmp_path, monkeypatch)
    assert Path(ref["path"]).parent.parent != tmp_path / "queue.json.evidence"
    result = callbacks["interpret"](TARGET, context)
    assert result.get("integration_status") == "accepted", result
    assert len(delivery.caller.results) == 1
    assert result["actual"]["run_id"] == delivery.caller.results[0]["run_id"]
    assert result["authority"] is False


@pytest.mark.parametrize("fault", ["foreign_raw", "tamper", "identity", "symlink"])
def test_imported_packet_never_falls_back_to_other_custody(tmp_path, monkeypatch, fault):
    delivery, callbacks, context, packet, ref = imported(tmp_path, monkeypatch)
    original = Path(ref["path"])
    if fault == "tamper":
        raw = packet["sections"]["ci_artifacts"]["content"]["artifacts"][0]["receipts"][0]["raw"]
        Path(raw["path"]).write_bytes(b"tampered")
    elif fault == "symlink":
        alias = original.with_name("alias.json")
        alias.symlink_to(original)
        context["prepare"]["packet_ref"] = {**ref, "path": str(alias)}
    else:
        changed = copy.deepcopy(packet)
        if fault == "identity":
            changed["target"] = "foreign/repo#9"
            changed["bindings"]["identity"] = evidence._hash(
                {key: changed.get(key) for key in ("target", "subject_pr", "head", "base")}
            )
        else:
            foreign, _, _ = delivery.caller.packet(delivery.current, tmp_path / "foreign.evidence")
            changed["sections"]["ci_artifacts"] = foreign["sections"]["ci_artifacts"]
        # Seal actual bytes; a self-consistent manifest cannot widen raw custody.
        from test_closer_pre_model_readiness import reseal_section

        for name in changed["sections"]:
            reseal_section(changed, name, original.parent.parent)
        assert evidence.validate_packet(changed)
        original.write_bytes(evidence._bytes(changed))
        context["prepare"]["packet_ref"] = {
            **ref,
            "sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
        }
    result = callbacks["interpret"](TARGET, context)
    assert result["status"] == "pending", result
    assert result["authority"] is False
    assert not delivery.caller.results
    assert not delivery.guard_calls and not delivery.closure_calls


def test_missing_explicit_custody_root_never_reads_or_falls_back(tmp_path, monkeypatch):
    delivery, _callbacks, context, packet, _ref = imported(tmp_path, monkeypatch)
    import closer_disposition

    monkeypatch.setattr(
        closer_disposition,
        "_safe_raw",
        lambda *_a, **_k: pytest.fail("missing root read raw bytes"),
    )
    value = dispatcher.data_only_evidence_context(
        packet, snapshot=delivery.current, readiness=None, retained_dir=None
    )
    receipts = value["sections"]["ci_receipt_inputs"]["content"]["receipts"]
    assert receipts and all(not row["content_supplied"] for row in receipts)
    assert not delivery.caller.results
