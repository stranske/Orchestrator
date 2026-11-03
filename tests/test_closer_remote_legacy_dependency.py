"""A legacy repository reservation is not a causal dependency for a remote PR effect."""

import json

import pytest
from test_closer_reasoning_boundary import native_world as native_world

import closer_dispatcher as dispatcher

TARGET = "o/r#850"
SOURCE = "o/r#849"


def legacy(
    tmp_path, *, status="uncertain", old_target="o/r#667", binding=None, claims=None, private=False
):
    operation = {
        "id": "retained-review-operation",
        "action": "respond_review",
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


@pytest.mark.parametrize("stage", ["act", "reconcile"])
def test_registered_legacy_barriers_receive_full_source_snapshot(native_world, monkeypatch, stage):
    import hashlib

    import closer_pipeline as pipeline

    world = native_world
    captured = {}

    class CapturedCallbacks(Exception):
        pass

    def capture(callbacks):
        captured.update(callbacks)
        raise CapturedCallbacks

    monkeypatch.setattr(pipeline, "_register_dispatcher_finite_callbacks", capture)
    with pytest.raises(CapturedCallbacks):
        dispatcher.main(world.argv)
    snapshot = {**world.snapshot, "pr_target": world.snapshot["target"]}
    retained = world.root / "full-alias-snapshot.json"
    retained.write_text(json.dumps(snapshot))
    compact = {key: snapshot[key] for key in ("target", "revision", "head")}
    assert "sources" not in compact and "pr_target" not in compact
    context = {
        "prepare": {
            "revision": snapshot["revision"],
            "snapshot": compact,
            "snapshot_ref": {
                "path": str(retained),
                "sha256": hashlib.sha256(retained.read_bytes()).hexdigest(),
            },
        },
        "action_state_path": str(world.root / "never-created-action-state.json"),
    }
    seen = []

    def dependency(_state, target, observed, **_kwargs):
        seen.append(observed)
        assert target == snapshot["target"]
        return {"status": "pending", "reason": "related private result"}

    monkeypatch.setattr(dispatcher, "_legacy_operation_dependency", dependency)
    captured[stage](snapshot["target"], context)
    assert len(seen) == 1
    assert seen[0]["sources"] == snapshot["sources"]
    assert seen[0]["pr_target"] == snapshot["pr_target"]
    assert world.events["effect"] == world.events["provider"] == 0


@pytest.mark.parametrize("status", ["submitted", "running", "uncertain"])
def test_same_target_native_advisory_contract_not_remote_write_dependency(tmp_path, status):
    from test_closer_native_admission_classification import native_row

    row = native_row(TARGET, action="review", capacity=False)
    row["operation"]["status"] = status
    path = tmp_path / "native.json"
    path.write_text(json.dumps({"schema": "closer-queue/v1", "round": {}, "items": {TARGET: row}}))
    before = path.read_bytes()
    assert (
        dispatcher._legacy_operation_dependency(path, TARGET, snapshot(), remote_only=True) is None
    )
    with pytest.raises(RuntimeError, match="original closer queue"):
        dispatcher._legacy_operation_dependency(path, TARGET, snapshot())
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "change",
    [
        {"requires_coordinator": True},
        {"operation_history": [{"status": "uncertain", "action": "respond_review"}]},
        {"operation_history": "malformed"},
    ],
)
def test_advisory_exception_never_borrows_private_or_mixed_history(tmp_path, change):
    from test_closer_native_admission_classification import native_row

    row = native_row(TARGET, action="review", capacity=False)
    row.update(change)
    path = tmp_path / "native.json"
    path.write_text(json.dumps({"schema": "closer-queue/v1", "round": {}, "items": {TARGET: row}}))
    before = path.read_bytes()
    with pytest.raises(RuntimeError):
        dispatcher._legacy_operation_dependency(path, TARGET, snapshot(), remote_only=True)
    assert path.read_bytes() == before
