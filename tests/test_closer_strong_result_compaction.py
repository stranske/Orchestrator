"""Real inert controlled strong callbacks keep scheduling state bounded."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from test_closer_disposition import TARGET
from test_closer_execution_contract_routes import (
    controlled_world as controlled_world,
)
from test_closer_execution_contract_routes import (
    inert_transport as inert_transport,
)
from test_closer_execution_contract_routes import (
    named,
    native_callback_case,
    prepared,
    scoped_answer,
)

import closer_evidence as evidence


@pytest.mark.parametrize("fault", [None, "metadata_write", "output_hash", "original_proof"])
@native_callback_case
def test_strong_callback_retains_metadata_once_and_keeps_original_guard(
    tmp_path, monkeypatch, fault
):
    delivery, callbacks, context = prepared(tmp_path, monkeypatch)
    row = named(context["interpret"])
    row["result"] = "UNKNOWN"
    original_retain = evidence._retain_bytes

    def retain(root, data, suffix):
        if suffix == "worker-metadata.json" and fault == "metadata_write":
            raise OSError("injected metadata retention failure")
        return original_retain(root, data, suffix)

    monkeypatch.setattr(evidence, "_retain_bytes", retain)
    delivery.strong_response = lambda envelope, *_: {
        "exit": 0,
        "output": json.dumps(scoped_answer(envelope["adjudication_request"], row)),
    }
    result = callbacks["exception"](TARGET, context)
    if fault == "metadata_write":
        assert result["status"] == "uncertain"
        assert result["classification"] == "completed_response_retention_failed"
        assert result["provider_outcome"]["actual"]["provider_completed"] is True
        return
    assert result["validated"]["status"] == "accepted", result
    worker = result["result"]["worker"]
    assert "_closer_controlled_proof" not in worker
    assert "data_only_origin" not in worker
    assert "request_metadata" not in worker
    metadata_ref = worker["metadata_ref"]
    raw = Path(metadata_ref["path"]).read_bytes()
    assert len(raw) == metadata_ref["bytes"]
    assert hashlib.sha256(raw).hexdigest() == metadata_ref["sha256"]
    metadata = json.loads(raw)
    assert metadata["_closer_controlled_proof"] == result["actual"]["_closer_controlled_proof"]
    assert metadata["data_only_origin"]["authority"] is False
    assert "output" not in metadata
    # Real source uses ensure_ascii=True/default separators in its ledger bound.
    assert len(json.dumps(result).encode()) <= 32768
    assert len(json.dumps([context["prepare"], context["interpret"], result]).encode()) <= 65536
    before = copy.deepcopy(result["actual"]["_closer_controlled_proof"])
    context["exception"] = result
    context["action_state_path"] = str(tmp_path / "action-queue.json")
    if fault == "output_hash":
        ref = worker["output_ref"]
        path = Path(ref["path"])
        raw = path.read_bytes()
        path.write_bytes(b"x" * len(raw))
    elif fault == "original_proof":
        result["actual"]["_closer_controlled_proof"]["expected"]["basis"]["request_id"] = "foreign"
    action = callbacks["act"](TARGET, context)
    if fault == "original_proof":
        # Actual callback scope detects changed stage identity before ACT can
        # read/integrate it. Do not bypass that earlier authority boundary.
        assert action["status"] == "blocked", action
        assert action["reason"] == "finite_frontier_identity_mismatch"
    elif fault:
        assert action["status"] == "uncertain", action
        assert action["classification"] == "completed_response_retention_failed"
    else:
        assert result["actual"]["_closer_controlled_proof"] == before
