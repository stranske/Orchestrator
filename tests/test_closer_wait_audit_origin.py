"""Deduplicated native refusal origins survive prospective read-only cadences."""

from __future__ import annotations

import copy
import time

import pytest
from test_closer_refusal_continuation import native_refusal

import claims
import closer_pipeline as pipeline

ORIGIN = "03fca" + "1" * 59
NEWER = "32d24" + "2" * 59


def seeded_deduplicated_refusal(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "private-state"))
    monkeypatch.setattr(claims, "holder", lambda *_: None)
    target, path, snapshot, receipt, _proof = native_refusal(
        tmp_path, "substantive_or_ambiguous_finding"
    )
    now = time.time()
    prepare = {"revision": snapshot["revision"], "identity": target, "snapshot": snapshot}
    row = {
        "status": "failed",
        "stage": "act",
        "error": "act returned unsupported or missing status",
        "revision": snapshot["revision"],
        "identity": target,
        "work_key": "old-work",
        "metrics": {"stage_seconds": {}, "errors": 0, "waits": 0},
        "results": {
            "prepare": copy.deepcopy(prepare),
            "act": {
                "status": "blocked",
                "reason": "substantive_or_ambiguous_finding",
                "next_trigger": pipeline.EVIDENCE_REFUSAL_TRIGGER,
                "queue_receipt": receipt,
            },
        },
    }
    held = "owner/held#99"
    held_row = {
        "status": "failed",
        "stage": "act",
        "error": "act returned unsupported or missing status",
        "revision": "held-revision",
        "identity": held,
        "work_key": "held-work",
        "metrics": {"stage_seconds": {}, "errors": 0, "waits": 0},
        "results": {"act": {"status": "blocked", "reason": "unknown effect"}},
    }
    original = {
        "created": now - 30,
        "deadline": now - 20,
        "assignment": sorted([target, held]),
        "targets": {target: row, held: held_row},
    }
    state = pipeline._empty()
    state["runs"] = {ORIGIN: copy.deepcopy(original)}
    pipeline._save(path, state)
    stopped = pipeline.validate_stop(path, [target, held], now=lambda: now)
    recovery = pipeline._expired_invalid_round_recovery(stopped, original, path, ORIGIN, now)
    assert recovery[0] and target in recovery[2]
    audit = recovery[5]["history_entry"]
    assert audit["targets"][target]["recovery"] == "validated_inner_wait"
    original_hash = audit["targets"][target]["item_sha256"]
    # Same actual failed return/native receipt, newer scheduler bookkeeping. The
    # exact material is deduplicated; the current raw item hash is different.
    newer = copy.deepcopy(original)
    newer["targets"][target]["last_scheduled"] = now - 10
    held_audit = audit["targets"][held]
    newer["targets"][held]["recovery_barrier"] = {
        "schema": "closer-unverified-act-barrier/v1",
        "source_run_id": ORIGIN,
        "item_sha256": held_audit["item_sha256"],
        "revision": held_row["revision"],
        "identity": held,
        "work_key": held_row["work_key"],
        "act_result_sha256": pipeline._digest(held_row["results"]["act"]),
    }
    newer["invalid_stop_history"] = [audit]
    state["runs"] = {NEWER: newer}
    pipeline._save(path, state)
    stopped = pipeline.validate_stop(path, [target, held], now=lambda: now)
    again = pipeline._expired_invalid_round_recovery(stopped, newer, path, NEWER, now)
    assert again[0] and target not in (again[5]["history_entry"] or {}).get("targets", {})
    assert pipeline._digest(newer["targets"][target]) != original_hash
    return path, target, prepare, now, audit, original_hash, newer


@pytest.mark.parametrize(
    "tamper", [None, "source", "hash", "act", "head", "revision", "identity", "missing_history"]
)
def test_original_deduplicated_wait_origin_survives_three_read_cadences(
    tmp_path, monkeypatch, tamper
):
    path, target, prepare, now, audit, original_hash, newer = seeded_deduplicated_refusal(
        tmp_path, monkeypatch
    )
    native = path.parent / f"{path.stem}.actions" / f"{pipeline._digest(target)}.json"
    native_bytes = native.read_bytes()
    calls = []

    def refresh(name, context):
        assert name == target
        calls.append(name)
        return copy.deepcopy(prepare)

    def forbidden(*a, **kw):
        pytest.fail("no provider or action may execute")

    for cadence in range(3):
        clock = now + cadence * 20
        result = pipeline.run_pipeline(
            path,
            [target],
            prepare=refresh,
            interpret=forbidden,
            exception=forbidden,
            act=forbidden,
            capacity_fn=lambda: {"io": 1, "low": 0, "exception": 0},
            deadline=clock + 10,
            now=lambda: clock,
        )
        if tamper and cadence == 1:
            assert result.get("reason") == "prior_run_has_no_valid_stop"
            assert len(calls) == 1
            return
        assert result.get("reason") != "prior_run_has_no_valid_stop", result
        current = max(pipeline._load(path)["runs"].values(), key=lambda r: r["created"])
        anchor = current["recovered_invalid_stop"]["targets"][target]
        assert (
            anchor.get("previous_run_id", current["recovered_invalid_stop"]["previous_run_id"])
            == ORIGIN
        )
        assert anchor["previous_item_sha256"] == original_hash
        assert anchor["previous_item_sha256"] != pipeline._digest(newer["targets"][target])
        assert current["targets"][target]["deferred_reason"] == "worker_capacity"
        assert current["invalid_stop_history"][0] == audit
        assert native.read_bytes() == native_bytes
        if tamper and cadence == 0:
            state = pipeline._load(path)
            r = max(state["runs"].values(), key=lambda r: r["created"])
            record = r["invalid_stop_history"][0]
            if tamper == "source":
                record["source_run_id"] = "other-origin"
            elif tamper == "hash":
                record["targets"][target]["item_sha256"] = "f" * 64
            elif tamper == "act":
                record["targets"][target]["act_result"]["reason"] = "tampered"
            elif tamper == "head":
                r["targets"][target]["results"]["prepare"]["snapshot"]["head"] = "b" * 40
            elif tamper == "revision":
                record["targets"][target]["revision"] = "other-revision"
            elif tamper == "identity":
                record["targets"][target]["identity"] = "other-identity"
            else:
                r["invalid_stop_history"] = []
            pipeline._save(path, state)
    assert len(calls) == 3


@pytest.mark.parametrize("tamper", ["source", "hash", "act"])
def test_unprovable_initial_audit_origin_holds_before_any_callback(tmp_path, monkeypatch, tamper):
    path, target, prepare, now, audit, original_hash, newer = seeded_deduplicated_refusal(
        tmp_path, monkeypatch
    )
    state = pipeline._load(path)
    row = state["runs"][NEWER]["invalid_stop_history"][0]
    if tamper == "source":
        row["source_run_id"] = None
    elif tamper == "hash":
        row["targets"][target]["item_sha256"] = "not-a-digest"
    else:
        row["targets"][target]["act_result"]["reason"] = "forged"
    pipeline._save(path, state)
    before = path.read_bytes()

    def forbidden(*a, **kw):
        pytest.fail("unproven origin must not run a fresh callback")

    result = pipeline.run_pipeline(
        path,
        [target],
        prepare=forbidden,
        interpret=forbidden,
        exception=forbidden,
        act=forbidden,
        capacity_fn=lambda: {"io": 1, "low": 0, "exception": 0},
        deadline=now + 10,
        now=lambda: now,
    )
    assert result["reason"] in {
        "validated_wait_audit_origin_unavailable",
        "prior_run_has_no_valid_stop",
    }
    assert path.read_bytes() == before


def test_predeadline_native_origin_record_is_truthful_and_refuses_any_barrier(
    tmp_path, monkeypatch
):
    path, target, _prepare, now, _audit, _hash, previous = seeded_deduplicated_refusal(
        tmp_path, monkeypatch
    )
    previous = copy.deepcopy(previous)
    previous["deadline"] = now + 100
    item = previous["targets"][target]
    assert pipeline._legacy_failed_act_wait_proof(path, target, item)
    recorded = pipeline._native_wait_origin_record(
        path, target, item, previous, NEWER, {"valid": False}, lambda: now
    )
    assert recorded["schema"] == pipeline.NATIVE_WAIT_AUDIT_SCHEMA
    assert recorded["boundary"] == "validated_nonmutating_native_wait"
    assert recorded["acceptance_authority"] is False
    assert recorded["source_run_id"] == NEWER
    assert recorded["source_deadline"] == now + 100 > recorded["recorded_at"]
    assert recorded["targets"][target]["item_sha256"] == pipeline._digest(item)
    assert recorded["targets"][target]["act_result"] == item["results"]["act"]
    for barrier in [{}, {"schema": "closer-unverified-act-barrier/v1"}, "unknown"]:
        item["recovery_barrier"] = barrier
        assert pipeline._legacy_failed_act_wait_proof(path, target, item)
        assert (
            pipeline._native_wait_origin_record(
                path, target, item, previous, NEWER, {}, lambda: now
            )
            is None
        )


def test_arbitrary_history_schema_cannot_supply_wait_origin(tmp_path, monkeypatch):
    _path, target, _prepare, _now, audit, _hash, previous = seeded_deduplicated_refusal(
        tmp_path, monkeypatch
    )
    audit = copy.deepcopy(audit)
    audit["schema"] = "arbitrary-recovery-hint/v1"
    assert (
        pipeline._validated_wait_audit_origin([audit], target, previous["targets"][target]) is None
    )
