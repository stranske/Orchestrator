"""A legacy repository reservation is not a causal dependency for a remote PR effect."""

import json

import pytest

import closer_dispatcher as dispatcher

TARGET = "o/r#850"
SOURCE = "o/r#849"


def legacy(
    tmp_path, *, status="uncertain", old_target="o/r#667", binding=None, claims=None, private=False
):
    operation = {
        "id": "retained-review-operation",
        "action": "review",
        "status": status,
        "repo_keys": ["o/r"],
        "binding": binding or {"pr_target": old_target, "source_targets": ["o/r#666"]},
        "claim_targets": claims or [old_target, "o/r#666"],
    }
    path = tmp_path / "legacy.json"
    path.write_text(
        json.dumps(
            {
                "schema": "closer-queue/v1",
                "round": {},
                "items": {old_target: {"operation": operation, "requires_coordinator": private}},
            }
        )
    )
    return path


def snapshot():
    return {"target": TARGET, "pr_target": TARGET, "sources": [{"target": SOURCE}]}


@pytest.mark.parametrize("status", ["submitted", "running", "uncertain"])
def test_qualified_remote_effect_ignores_unrelated_repository_only_dependency(tmp_path, status):
    path = legacy(tmp_path, status=status)
    before = path.read_bytes()
    assert (
        dispatcher._legacy_operation_dependency(path, TARGET, snapshot(), remote_only=True) is None
    )
    assert path.read_bytes() == before
    # The same unresolved operation still protects actual repository users.
    with pytest.raises(RuntimeError, match="original closer queue"):
        dispatcher._legacy_operation_dependency(path, TARGET, snapshot())
    assert path.read_bytes() == before


@pytest.mark.parametrize("association", ["target", "source", "claim", "bound_pr", "bound_source"])
def test_remote_effect_retains_exact_related_operation_dependency(tmp_path, association):
    old_target = (
        TARGET if association == "target" else SOURCE if association == "source" else "o/r#667"
    )
    binding = {
        "pr_target": TARGET if association == "bound_pr" else old_target,
        "source_targets": [SOURCE] if association == "bound_source" else [],
    }
    claims = [TARGET] if association == "claim" else [old_target]
    path = legacy(tmp_path, old_target=old_target, binding=binding, claims=claims)
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="original closer queue"):
        dispatcher._legacy_operation_dependency(path, TARGET, snapshot(), remote_only=True)
    assert path.read_bytes() == before


@pytest.mark.parametrize("related", [False, True])
def test_completed_private_result_blocks_only_its_remote_target_relationship(tmp_path, related):
    path = legacy(
        tmp_path, status="completed", old_target=TARGET if related else "o/r#667", private=True
    )
    before = path.read_bytes()
    if related:
        with pytest.raises(RuntimeError, match="existing coordinator"):
            dispatcher._legacy_operation_dependency(path, TARGET, snapshot(), remote_only=True)
    else:
        assert (
            dispatcher._legacy_operation_dependency(path, TARGET, snapshot(), remote_only=True)
            is None
        )
        with pytest.raises(RuntimeError, match="existing coordinator"):
            dispatcher._legacy_operation_dependency(path, TARGET, snapshot())
    assert path.read_bytes() == before


def test_corrupt_legacy_queue_never_qualifies_as_absent(tmp_path):
    path = tmp_path / "legacy.json"
    path.write_text("{")
    with pytest.raises(RuntimeError, match="cannot be safely reconciled"):
        dispatcher._legacy_operation_dependency(path, TARGET, snapshot(), remote_only=True)
    assert path.read_text() == "{"
