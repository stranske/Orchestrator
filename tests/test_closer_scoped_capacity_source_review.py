"""Capacity enumeration changes cannot conceal unrelated historical source drift."""

import hashlib

import closer_semantic_native_integration_support as support
import pytest


def test_exact_capacity_successor_rejects_unreviewed_code_and_fixture_changes(
    tmp_path, monkeypatch
):
    source = support.source_path("closer_dispatcher.py").read_text()
    reviewed = support.scoped_capacity_reference()
    restored = support.before_scoped_capacity_source(source)
    assert (
        hashlib.sha256(restored.encode()).hexdigest()
        == reviewed["dispatcher"]["before_file_sha256"]
    )
    for changed in (
        source + "\n# unrelated source change\n",
        source.replace("def _run_staged_pipeline(", "def unreviewed_pipeline(", 1),
        source.replace("declared_ids = set(declared)", "declared_ids = set()", 1),
        source.replace(
            reviewed["dispatcher"]["validation_function"]["after_fragment"],
            reviewed["dispatcher"]["validation_function"]["before_fragment"],
            1,
        ),
    ):
        assert changed != source
        with pytest.raises(AssertionError):
            support.before_scoped_capacity_source(changed)
    fixture = tmp_path / "altered.json"
    fixture.write_bytes(support.SCOPED_CAPACITY_MANIFEST.read_bytes() + b"\n")
    monkeypatch.setattr(support, "SCOPED_CAPACITY_MANIFEST", fixture)
    with pytest.raises(AssertionError):
        support.scoped_capacity_reference()
