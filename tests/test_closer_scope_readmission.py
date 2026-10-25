"""Audited no-callback ACT holds may request fresh normal admission, never replay effects."""

import fcntl
import json

import pytest
from test_closer_finite_reentry import TARGET, apply, fixture, read, scope_proof

import closer_dispatcher as dispatcher
import closer_finite_reentry as retry
import closer_pipeline as pipeline
import closer_quarantine as quarantine
import closer_resources as resources


def held(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    path, frontier, doc = fixture(tmp_path, "act", "unknown")
    root = path.parent.parent.parent
    global_path = tmp_path / "queue.pipeline.json"
    resources.open_root(global_path, pipeline_schema=pipeline.SCHEMA)
    lease = resources.acquire(root, TARGET, owner="terminal-source", finite_frontier=frontier)
    lease.release()
    snapshot = {
        "target": TARGET,
        "kind": "pr",
        "revision": "rev",
        "head": "a" * 40,
        "head_branch": "feature",
        "head_repository": "owner/repo",
        "sources": [],
        "labels": [],
    }
    snapshot_path = tmp_path / "prepared-snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot))
    doc["runs"] = {"historical": {"targets": {}}}
    row = doc["frontiers"]["frozen"]["items"][TARGET]
    row["results"] = {
        "prepare": {
            "revision": "rev",
            "identity": TARGET,
            "snapshot_ref": {"path": str(snapshot_path), "sha256": retry._read(snapshot_path)[1]},
            "proposal": {"action": "merge"},
            "native_effect_contract": {
                "kind": "native_remote_only",
                "target": TARGET,
                "revision": "rev",
                "head": "a" * 40,
                "action": "merge",
            },
        },
        "interpret": {
            "classification": "deterministic_merge",
            "authority": False,
            "substantive_needs": [],
        },
    }
    location, proof = scope_proof(path, doc)
    derived = dispatcher._prepared_resource_keys(
        TARGET, row["results"]["prepare"], snapshot, {}, infer_workspace=False
    )
    proof.update(
        snapshot=quarantine.scope_snapshot(global_path),
        legacy_stop_authority=False,
        purpose="remote_effect",
        keys=resources._scope_keys(
            TARGET, row["results"], lambda *_: derived, infer_workspaces=False
        ),
    )
    location.write_text(json.dumps(proof))
    proposal, result = apply(path, frontier, "classify-positive-no-callback-proof")
    return path, frontier, global_path, proposal, result


def request(path, frontier, event="scope-now-admits"):
    return apply(path, frontier, event)


def test_exact_scope_progress_rearms_act_without_rewinding_prior_stages(tmp_path, monkeypatch):
    path, frontier, global_path, _, origin = held(tmp_path, monkeypatch)
    before = read(path)
    original = json.loads(path.read_text())
    proposal, result = request(path, frontier)
    assert proposal["kind"] == "scope_no_callback_readmission"
    after = read(path)
    assert after["stage"] == "act" and after["stage_status"] == "queued"
    assert after["results"] == before["results"]
    assert after["reentry_history"][-1]["previous"] == {
        k: v for k, v in before.items() if k != "reentry_history"
    }
    assert json.loads(path.read_text())["runs"] == original["runs"]
    assert json.loads(path.read_text())["frontiers"]["frozen"]["finite_frontier"] == frontier
    assert retry.apply(
        path,
        frontier=frontier,
        target=TARGET,
        event="scope-now-admits",
        expected_plan_sha256=proposal["plan_sha256"],
    )["idempotent"]
    with pytest.raises(ValueError):
        retry.plan(path, frontier=frontier, target=TARGET, event="another-nonce")
    assert retry._read(origin["audit"])[0]["after"] == before


# Inert registered-callback seam only: keep the real scheduler, lease, census,
# reservations and guards. No production adapter, GitHub or provider is invoked.
_remote_only_effect = dispatcher._remote_only_effect


def run_real(path, frontier, global_path, monkeypatch, callback, *, pause=None):
    monkeypatch.setattr(pipeline, "_require_dispatcher_finite_callbacks", lambda *_: None)

    def not_repeated(*_):
        pytest.fail("completed PREPARE/INTERPRET must not rerun")

    # remote_act validates the real native-effect capability from callback globals.
    def act(target, context):
        return callback(target, context)

    return resources.run_scoped(
        global_path,
        [TARGET],
        finite_frontier=frontier,
        request_timeout_seconds=5,
        prepare=not_repeated,
        interpret=not_repeated,
        exception=not_repeated,
        act=act,
        capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
        resource_keys=lambda _t, c: dispatcher._prepared_resource_keys(
            TARGET,
            c["prepare"],
            retry._receipt(c["prepare"]["snapshot_ref"]),
            {},
            infer_workspace=False,
        ),
        paused_fn=pause,
    )


def test_real_finite_reentry_performs_fresh_scope_before_one_act(tmp_path, monkeypatch):
    path, frontier, global_path, _, _ = held(tmp_path, monkeypatch)
    request(path, frontier)
    seen = []
    real_problem = quarantine.scope_problem

    def observe(snapshot, target, keys, **kwargs):
        seen.append(("scope", kwargs.get("purpose")))
        return real_problem(snapshot, target, keys, **kwargs)

    monkeypatch.setattr(quarantine, "scope_problem", observe)

    def callback(*_):
        assert ("scope", "remote_effect") in seen
        seen.append(("act", None))
        return {"status": "completed", "target_complete": True}

    result = run_real(path, frontier, global_path, monkeypatch, callback)
    assert result["targets"][TARGET]["status"] == "completed", result
    assert sum(k == "act" for k, _ in seen) == 1
    assert "reentry_ready" not in read(path)


def explicit_blocker(global_path, key="repo:owner/repo"):
    dest = resources._key_dir(resources.scoped_root(global_path), key) / "legacy-intent.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(
            {"key": key, "target": TARGET, "owner": "unresolved-foreign", "state": "unknown"}
        )
    )
    return dest


@pytest.mark.parametrize("key", ["repo:owner/repo", "pr:" + TARGET])
def test_current_explicit_quarantine_is_never_waived(tmp_path, monkeypatch, key):
    path, frontier, global_path, _, _ = held(tmp_path, monkeypatch)
    blocked = explicit_blocker(global_path, key)
    before = path.read_bytes(), blocked.read_bytes()
    with pytest.raises(ValueError, match="scope_readmission_still_refused"):
        request(path, frontier)
    assert (path.read_bytes(), blocked.read_bytes()) == before


def test_foreign_intent_after_plan_refuses_apply_without_changing_row(tmp_path, monkeypatch):
    path, frontier, global_path, _, _ = held(tmp_path, monkeypatch)
    proposal = retry.plan(path, frontier=frontier, target=TARGET, event="x")
    explicit_blocker(global_path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="scope_readmission_still_refused"):
        retry.apply(
            path,
            frontier=frontier,
            target=TARGET,
            event="x",
            expected_plan_sha256=proposal["plan_sha256"],
        )
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "change",
    [
        "missing_audit",
        "changed_audit",
        "proof_hash",
        "head",
        "revision",
        "provider",
        "paused",
        "operation",
    ],
)
def test_changed_or_uncertain_lineage_is_not_rearmed(tmp_path, monkeypatch, change):
    path, frontier, _, _, origin = held(tmp_path, monkeypatch)
    doc = json.loads(path.read_text())
    row = doc["frontiers"]["frozen"]["items"][TARGET]
    if change == "missing_audit":
        __import__("pathlib").Path(origin["audit"]).unlink()
    elif change == "changed_audit":
        loc = __import__("pathlib").Path(origin["audit"])
        data = json.loads(loc.read_text())
        data["after_sha256"] = "0" * 64
        loc.write_text(json.dumps(data))
    elif change == "proof_hash":
        loc = __import__("pathlib").Path(row["scope_proof"]["path"])
        loc.write_text(loc.read_text() + " ")
    elif change == "head":
        loc = __import__("pathlib").Path(row["results"]["prepare"]["snapshot_ref"]["path"])
        data = json.loads(loc.read_text())
        data["head"] = "b" * 40
        loc.write_text(json.dumps(data))
    elif change == "revision":
        row["revision"] = "changed"
    elif change == "provider":
        row["provider_outcome_uncertain"] = True
    elif change == "paused":
        row["status"] = "paused"
    elif change == "operation":
        inner = path.parent / "state.actions" / (pipeline._digest(TARGET) + ".json")
        inner.parent.mkdir(exist_ok=True)
        inner.write_text(
            json.dumps(
                {
                    "schema": "closer-queue/v1",
                    "items": {TARGET: {"operation": {"status": "running"}}},
                }
            )
        )
    if change in {"revision", "provider", "paused"}:
        path.write_text(json.dumps(doc))
    before = path.read_bytes()
    with pytest.raises((ValueError, OSError)):
        request(path, frontier)
    assert path.read_bytes() == before


@pytest.mark.parametrize("lock_name", ["lease.lock", "state.json.runlock"])
def test_actual_concurrent_owner_refuses_apply(tmp_path, monkeypatch, lock_name):
    path, frontier, _, _, _ = held(tmp_path, monkeypatch)
    proposal = retry.plan(path, frontier=frontier, target=TARGET, event="x")
    with (path.parent / lock_name).open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(resources.ResourceBlocked, match="active_finite_writer"):
            retry.apply(
                path,
                frontier=frontier,
                target=TARGET,
                event="x",
                expected_plan_sha256=proposal["plan_sha256"],
            )


def test_changed_lease_refuses_plan_cas(tmp_path, monkeypatch):
    path, frontier, _, _, _ = held(tmp_path, monkeypatch)
    proposal = retry.plan(path, frontier=frontier, target=TARGET, event="x")
    lease = path.parent / "lease.json"
    value = json.loads(lease.read_text())
    value["epoch"] += 1
    lease.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="reentry_plan_changed"):
        retry.apply(
            path,
            frontier=frontier,
            target=TARGET,
            event="x",
            expected_plan_sha256=proposal["plan_sha256"],
        )


def test_real_act_guard_refuses_new_quarantine_after_ready_without_callback(tmp_path, monkeypatch):
    path, frontier, global_path, _, _ = held(tmp_path, monkeypatch)
    request(path, frontier)
    actual_ready = retry.ready

    def ready_then_block(*args):
        result = actual_ready(*args)
        explicit_blocker(global_path)
        return result

    monkeypatch.setattr(retry, "ready", ready_then_block)
    seen = []
    run_real(path, frontier, global_path, monkeypatch, lambda *_: seen.append("BAD"))
    row = read(path)
    assert seen == []
    assert row["status"] == "held" and row["deferred_reason"] == "scope_quarantined", row
    assert row["resource_evidence"]["confirmed_not_started"] is True
    assert "reentry_ready" not in row
    # The ordinary existing no-start reconciliation may classify the new proof;
    # that classification never queues ACT, and another nonce cannot reopen it.
    proposal, _ = request(path, frontier, "classify-new-no-start")
    assert proposal["kind"] == "pre_callback_scope_refusal"
    assert read(path)["stage_status"] == "completed"
    assert "reentry_ready" not in read(path)
    with pytest.raises(ValueError):
        request(path, frontier, "another-nonce")


def test_inventory_census_capability_cannot_escape_lock_root_thread_or_lifetime(tmp_path):
    import threading

    root = tmp_path / "owned.scoped"
    with resources.inventory_lock(root, exclusive=True) as owner:
        with resources.inventory_census(root, owner=owner):
            pass
        with pytest.raises(resources.ResourceBlocked):
            with resources.inventory_census(tmp_path / "foreign.scoped", owner=owner):
                pass
        errors = []

        def foreign_thread():
            try:
                with resources.inventory_census(root, owner=owner):
                    pass
            except resources.ResourceBlocked:
                errors.append("refused")

        thread = threading.Thread(target=foreign_thread)
        thread.start()
        thread.join(timeout=2)
        assert errors == ["refused"]
    with pytest.raises(resources.ResourceBlocked):
        with resources.inventory_census(root, owner=owner):
            pass


def test_inner_lock_is_held_through_outer_commit_and_audit(tmp_path, monkeypatch):
    path, frontier, _, _, _ = held(tmp_path, monkeypatch)
    inner = path.parent / "state.actions" / (pipeline._digest(TARGET) + ".json")
    proposal = retry.plan(path, frontier=frontier, target=TARGET, event="locked")
    original_save = pipeline._save_snapshot
    observed = []

    def inspect_lock(*args):
        with inner.with_name(inner.name + ".lock").open("a+") as handle:
            with pytest.raises(BlockingIOError):
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        observed.append(True)
        return original_save(*args)

    monkeypatch.setattr(pipeline, "_save_snapshot", inspect_lock)
    retry.apply(
        path,
        frontier=frontier,
        target=TARGET,
        event="locked",
        expected_plan_sha256=proposal["plan_sha256"],
    )
    assert observed == [True] and not inner.exists()


def test_ready_revalidates_origin_and_inner_under_real_new_lease(tmp_path, monkeypatch):
    path, frontier, _, _, _ = held(tmp_path, monkeypatch)
    request(path, frontier)
    inner = path.parent / "state.actions" / (pipeline._digest(TARGET) + ".json")
    inner.write_text(
        json.dumps(
            {
                "schema": "closer-queue/v1",
                "items": {TARGET: {"operation_history": [{"status": "completed"}]}},
            }
        )
    )
    lease = resources.acquire(
        path.parent.parent.parent, TARGET, owner="normal-resume", finite_frontier=frontier
    )
    try:
        with resources.fenced(lease), pytest.raises(ValueError, match="inner_operation"):
            retry.ready(path, frontier, TARGET, read(path))
    finally:
        lease.release()
    assert "reentry_ready" in read(path)


def test_ready_cannot_consume_changed_audit_row(tmp_path, monkeypatch):
    path, frontier, _, _, _ = held(tmp_path, monkeypatch)
    _, outcome = request(path, frontier)
    location = __import__("pathlib").Path(outcome["audit"])
    audit = json.loads(location.read_text())
    audit["after_sha256"] = "0" * 64
    location.write_text(json.dumps(audit))
    lease = resources.acquire(
        path.parent.parent.parent, TARGET, owner="normal-resume", finite_frontier=frontier
    )
    try:
        with resources.fenced(lease), pytest.raises(ValueError, match="does_not_bind"):
            retry.ready(path, frontier, TARGET, read(path))
    finally:
        lease.release()


def test_scope_block_after_apply_cannot_enter_callback_or_clear_foreign_intent(
    tmp_path, monkeypatch
):
    path, frontier, global_path, _, _ = held(tmp_path, monkeypatch)
    request(path, frontier)
    blocked = explicit_blocker(global_path)
    original = blocked.read_bytes()
    seen = []
    run_real(path, frontier, global_path, monkeypatch, lambda *_: seen.append("BAD"))
    assert seen == [] and blocked.read_bytes() == original
    assert read(path)["stage_status"] == "queued"  # untouched READY, not falsely consumed


def test_paused_normal_pipeline_does_not_enter_callback(tmp_path, monkeypatch):
    path, frontier, global_path, _, _ = held(tmp_path, monkeypatch)
    request(path, frontier)
    seen = []
    result = run_real(
        path, frontier, global_path, monkeypatch, lambda *_: seen.append("BAD"), pause=lambda: True
    )
    assert seen == [] and result["targets"][TARGET]["status"] != "completed"
