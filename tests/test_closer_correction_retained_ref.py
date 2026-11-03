"""Shared strict reference identity; real custody I/O, no model/native actions."""

import hashlib
from pathlib import Path

import pytest

import closer_correction_classification as contract
import closer_correction_producer as producer


@pytest.mark.parametrize("suffix", ["correction-request.json", "correction-decision.json"])
def test_reference_matches_actual_retention_and_original_utf8_bytes(tmp_path, suffix):
    root = tmp_path.resolve()
    value = {"message": 'café "quote"\n', "a": [True, None, 1.5]}
    raw = b'{"a":[true,null,1.5],"message":"caf\xc3\xa9 \\"quote\\"\\n"}'
    sha = hashlib.sha256(raw).hexdigest()
    expected = {
        "path": str(root / "raw" / sha[:2] / (sha + "." + suffix)),
        "sha256": sha,
        "bytes": len(raw),
    }
    # Derivation is read-only even before original custody exists.
    assert producer.retained_ref(root, value, suffix) == expected
    assert list(root.iterdir()) == []
    retained = producer.retain(root, value, suffix)
    assert retained == expected
    assert Path(retained["path"]).read_bytes() == raw
    # Dictionary insertion order does not create another retained identity.
    assert producer.retained_ref(root, dict(reversed(list(value.items()))), suffix) == retained
    before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert producer.retain(root, value, suffix) == retained
    assert {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_value_is_refused_by_both_paths_without_writes(tmp_path, bad):
    root = tmp_path.resolve()
    for function in (producer.retained_ref, producer.retain):
        with pytest.raises(ValueError):
            function(root, {"nested": [{"value": bad}]}, "correction-decision.json")
        assert list(root.iterdir()) == []


def test_oversized_value_is_refused_before_custody_write(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    monkeypatch.setattr(contract, "MAX_BYTES", 128)
    for function in (producer.retained_ref, producer.retain):
        with pytest.raises(contract.ContractError, match="exceeds bound"):
            function(root, {"value": "x" * 129}, "correction-decision.json")
        assert list(root.iterdir()) == []
