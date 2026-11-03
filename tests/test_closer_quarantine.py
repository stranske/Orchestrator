"""Synthetic original-byte census and closed-world scope contracts."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

import closer_pipeline as pipeline
import closer_quarantine as quarantine
import closer_resources as resources


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def keys(target="owner/healthy#2"):
    return ["pr:" + target, "repo:" + target.split("#")[0]]


def ledger(tmp_path, row=None, target="owner/held#1"):
    path = tmp_path / "dispatch.json.pipeline.json"
    doc = pipeline._empty()
    if row is not None:
        doc["runs"]["origin"] = {"targets": {target: row}}
    write(path, doc)
    return path


def ad(path, key, target="owner/held#1", name="legacy-intent.json"):
    destination = resources._key_dir(resources.scoped_root(path), key) / name
    write(destination, {"key": key, "target": target, "owner": "prior-owner", "state": "unknown"})
    return destination


def test_empty_census_allows_disjoint_without_creating_paths(tmp_path):
    path = tmp_path / "absent.json"
    snapshot = quarantine.scope_snapshot(path)
    assert not snapshot["global_reasons"] and not snapshot["quarantine_keys"]
    assert quarantine.scope_problem(snapshot, "owner/healthy#2", keys()) is None
    assert quarantine.scope_problem(snapshot, "extension-target", []) is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("where", ["run", "carry"])
@pytest.mark.parametrize(
    "row",
    [
        {"status": "queued", "stage": "exception"},
        {"status": "running", "stage": "act"},
        {"status": "uncertain", "stage": "reconcile"},
        {"status": "failed", "stage": "interpret", "recovery_barrier": {}},
        {
            "status": "deferred",
            "stage": "interpret",
            "provider_outcome_uncertain": {"run": "unknown"},
        },
        {"status": "queued", "stage": "interpret", "results": []},
        "damaged item",
    ],
)
def test_anchored_item_failures_quarantine_repo_without_blocking_disjoint(tmp_path, row, where):
    path = ledger(tmp_path, row)
    if where == "carry":
        doc = json.loads(path.read_text())
        doc["carry"] = doc["runs"].pop("origin")["targets"]
        write(path, doc)
    before = path.read_bytes()
    snapshot = quarantine.scope_snapshot(path)
    assert not snapshot["global_reasons"]
    assert "repo:owner/held" in snapshot["quarantine_keys"]
    assert snapshot["items"][0]["item_sha256"] == quarantine._digest(row)
    assert snapshot["items"][0]["source_sha256"] == hashlib.sha256(before).hexdigest()
    assert quarantine.scope_problem(snapshot, "owner/held#9", keys("owner/held#9"))
    assert quarantine.scope_problem(snapshot, "owner/healthy#2", keys()) is None
    assert quarantine.scope_problem(snapshot, "extension-target", [])
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "row",
    [
        {"status": "queued", "stage": "prepare"},
        {"status": "queued", "stage": "interpret"},
        {"status": "failed", "stage": "interpret", "error": "ordinary local parse error"},
        {"status": "completed", "stage": "act"},
    ],
)
def test_ordinary_local_failure_and_preparation_not_quarantined(tmp_path, row):
    snapshot = quarantine.scope_snapshot(ledger(tmp_path, row))
    assert not snapshot["quarantine_keys"] and not snapshot["global_reasons"]


@pytest.mark.parametrize(
    "key",
    [
        "pr:owner/linked#8",
        "issue:owner/linked#9",
        "branch:owner/linked@work",
        "worktree",
    ],
)
def test_retained_ads_fence_exact_alias_and_preserve_original_bytes(tmp_path, key):
    path = ledger(tmp_path)
    key = "worktree:" + str(tmp_path / "checkout") if key == "worktree" else key
    source = ad(path, key)
    before = source.read_bytes()
    snapshot = quarantine.scope_snapshot(path)
    inventory = keys() + [key]
    if key.startswith(("pr:", "issue:", "branch:")):
        inventory.append("repo:owner/linked")
    assert quarantine.scope_problem(snapshot, "owner/healthy#2", inventory).startswith(
        "quarantined_resource:"
    )
    assert quarantine.scope_problem(snapshot, "owner/healthy#2", keys()) is None
    assert source.read_bytes() == before


def test_prepared_cross_repo_source_branch_and_worktree_retained_in_quarantine(tmp_path):
    row = {
        "status": "queued",
        "stage": "exception",
        "results": {
            "prepare": {
                "snapshot": {
                    "sources": [{"target": "owner/linked#9"}],
                    "pr_target": "owner/linked#8",
                    "head_repository": "owner/linked",
                    "head_branch": "feature",
                    "worktree": str(tmp_path / "checkout"),
                }
            }
        },
    }
    snapshot = quarantine.scope_snapshot(ledger(tmp_path, row))
    assert not snapshot["global_reasons"]
    for value in [
        "issue:owner/linked#9",
        "pr:owner/linked#8",
        "branch:owner/linked@feature",
        "worktree:" + str(tmp_path / "checkout"),
    ]:
        assert value in snapshot["quarantine_keys"]


@pytest.mark.parametrize("damage", ["json", "null", "runs", "target", "history", "archive"])
def test_unbounded_shared_damage_holds_all(tmp_path, damage):
    path = ledger(tmp_path)
    doc = json.loads(path.read_text())
    if damage == "json":
        path.write_text("{")
    elif damage == "null":
        write(path, None)
    else:
        doc["runs"]["origin"] = {"targets": {}}
        if damage == "runs":
            doc["runs"] = []
        elif damage == "target":
            doc["runs"]["origin"]["targets"]["unbound"] = {"status": "running"}
        elif damage == "history":
            doc["runs"]["origin"]["invalid_stop_history"] = "bad"
        else:
            doc["runs"]["origin"]["invalid_stop_archive"] = {"sha256": "a" * 64, "count": 1}
        write(path, doc)
    snapshot = quarantine.scope_snapshot(path)
    assert snapshot["global_reasons"]
    assert quarantine.scope_problem(snapshot, "owner/healthy#2", keys())


@pytest.mark.parametrize("damage", ["null", "key", "directory", "symlink"])
def test_unbounded_or_aliased_ad_holds_all(tmp_path, damage):
    path = ledger(tmp_path)
    source = ad(path, "pr:owner/held#1")
    if damage == "null":
        write(source, None)
    elif damage == "key":
        write(source, {"key": "not-a-key", "target": "owner/held#1"})
    elif damage == "directory":
        source.parent.rename(source.parent.with_name("wrong-digest"))
    else:
        original = source.read_bytes()
        source.unlink()
        elsewhere = tmp_path / "redirect"
        elsewhere.write_bytes(original)
        source.symlink_to(elsewhere)
    assert quarantine.scope_snapshot(path)["global_reasons"]


@pytest.mark.parametrize("change", ["ledger", "new-ad", "removed-ad", "ad-bytes"])
def test_source_changes_invalidate_prior_scope_even_if_semantics_same(tmp_path, change):
    path = ledger(tmp_path)
    source = ad(path, "pr:owner/held#1")
    snapshot = quarantine.scope_snapshot(path)
    if change == "ledger":
        path.write_bytes(path.read_bytes() + b" ")
    elif change == "new-ad":
        ad(path, "issue:owner/held#3")
    elif change == "removed-ad":
        source.unlink()
    else:
        source.write_bytes(source.read_bytes() + b" ")
    assert quarantine.scope_problem(snapshot, "owner/healthy#2", keys()) == "scope_sources_changed"


@pytest.mark.parametrize(
    "row,held",
    [
        ({"status": "queued", "operation": None}, True),
        ({"status": "complete", "operation": {"status": "uncertain"}}, True),
        ({"status": "complete", "operation": {"status": "completed"}}, False),
        ({"status": "deferred", "action": "wait", "eligible": False, "operation": None}, False),
        ({"status": "complete", "operation_history": [{"status": "running"}]}, True),
    ],
)
def test_exact_raw_queue_sibling_unknowns_preserved_but_known_terminal_not_fenced(
    tmp_path, row, held
):
    path = ledger(tmp_path)
    raw = Path(str(path)[: -len(".pipeline.json")])
    write(raw, {"schema": "closer-queue/v1", "items": {"owner/held#1": row}, "round": {}})
    before = raw.read_bytes()
    snapshot = quarantine.scope_snapshot(path)
    assert not snapshot["global_reasons"]
    assert ("repo:owner/held" in snapshot["quarantine_keys"]) is held
    assert raw.read_bytes() == before


def test_unrelated_sibling_not_read_and_corrupt_exact_raw_queue_is_global(tmp_path):
    path = ledger(tmp_path)
    unrelated = tmp_path / "unrelated.json"
    unrelated.write_text("not JSON")
    assert not quarantine.scope_snapshot(path)["global_reasons"]
    Path(str(path)[: -len(".pipeline.json")]).write_text("not JSON")
    assert quarantine.scope_snapshot(path)["global_reasons"]


@pytest.mark.parametrize(
    "inventory",
    [[], ["pr:owner/healthy#2"], ["repo:owner/healthy"], ["unknown:value"], ["worktree:relative"]],
)
def test_incomplete_or_unbounded_prepared_inventory_never_proves_disjoint(tmp_path, inventory):
    snapshot = quarantine.scope_snapshot(ledger(tmp_path))
    assert quarantine.scope_problem(snapshot, "owner/healthy#2", inventory)


def test_snapshot_metadata_tampering_rejected(tmp_path):
    snapshot = quarantine.scope_snapshot(
        ledger(tmp_path, {"status": "queued", "stage": "exception"})
    )
    snapshot["quarantine_keys"] = []
    assert quarantine.scope_problem(snapshot, "owner/healthy#2", keys()) == "invalid_scope_snapshot"


def test_damaged_anchored_item_history_is_repo_bounded_not_shared_global(tmp_path):
    path = ledger(tmp_path)
    doc = json.loads(path.read_text())
    doc["runs"]["origin"] = {
        "targets": {},
        "invalid_stop_history": [{"targets": {"owner/held#1": {"item_sha256": "damaged"}}}],
    }
    write(path, doc)
    snapshot = quarantine.scope_snapshot(path)
    assert not snapshot["global_reasons"]
    assert "repo:owner/held" in snapshot["quarantine_keys"]
    assert quarantine.scope_problem(snapshot, "owner/healthy#2", keys()) is None


def test_clean_extension_ledger_and_fresh_shard_remain_supported(tmp_path):
    import time

    path = ledger(tmp_path, {"status": "completed", "stage": "act"}, "extension#A")
    root = resources.open_root(path, pipeline_schema=pipeline.SCHEMA)
    lease = resources.acquire(
        root, "extension#A", owner="extension-owner", expires_at=time.time() + 60
    )
    try:
        doc = pipeline._empty()
        doc["runs"]["new"] = {
            "created": time.time(),
            "targets": {
                "extension#A": {
                    "status": "running",
                    "stage": "act",
                    "last_scheduled": time.time(),
                    "scope_lease": {
                        "target": lease.target,
                        "owner": lease.owner,
                        "epoch": lease.epoch,
                    },
                }
            },
        }
        write(lease.state_path, doc)
        snapshot = quarantine.scope_snapshot(path)
        assert not snapshot["global_reasons"] and not snapshot["quarantine_keys"]
        assert quarantine.scope_problem(snapshot, "extension#A", []) is None
    finally:
        lease.release()


def test_ancestor_alias_canonicalized_but_leaf_alias_refused(tmp_path):
    directory = tmp_path / "real"
    directory.mkdir()
    path = ledger(directory)
    alias = tmp_path / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    snapshot = quarantine.scope_snapshot(alias / path.name)
    assert snapshot["state_path"] == str(path)
    assert not snapshot["global_reasons"]
    leaf = tmp_path / "leaf"
    leaf.symlink_to(path)
    assert quarantine.scope_snapshot(leaf)["global_reasons"]


def test_bare_scope_refusal_label_is_not_no_effect_proof(tmp_path):
    row = {"status": "deferred", "stage": "interpret", "deferred_reason": "scope_quarantined"}
    snapshot = quarantine.scope_snapshot(ledger(tmp_path, row))
    assert "repo:owner/held" in snapshot["quarantine_keys"]
    assert not snapshot["global_reasons"]


def test_authorized_workspaces_close_cross_repository_quarantine_aliases(tmp_path):
    path = ledger(tmp_path, {"status": "queued", "stage": "exception"})
    workspace = tmp_path / "shared-checkout"
    mapping = {"owner/held": str(workspace), "owner/healthy": str(workspace)}
    snapshot = quarantine.scope_snapshot(path, workspaces=mapping)
    assert not snapshot["global_reasons"]
    assert snapshot["workspaces"] == mapping
    assert "worktree:" + str(workspace) in snapshot["quarantine_keys"]
    assert (
        quarantine.scope_problem(snapshot, "owner/healthy#2", keys())
        == "incomplete_resource_inventory"
    )
    assert quarantine.scope_problem(
        snapshot, "owner/healthy#2", keys() + ["worktree:" + str(workspace)]
    )
    assert quarantine.scope_problem(snapshot, "owner/other#2", keys("owner/other#2")) is None
    # Authorized mappings pin physical identities rather than spelling aliases.
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    alias = tmp_path / "mapped"
    alias.symlink_to(first, target_is_directory=True)
    aliased = quarantine.scope_snapshot(path, workspaces={"owner/held": str(alias)})
    assert aliased["workspaces"]["owner/held"] == str(first)


@pytest.mark.parametrize("mapping", [{"owner/held": "relative"}, {"unbound": "/tmp/work"}, []])
def test_unbounded_workspace_mapping_holds_globally(tmp_path, mapping):
    snapshot = quarantine.scope_snapshot(ledger(tmp_path), workspaces=mapping)
    assert snapshot["global_reasons"]


def refusal(tmp_path):
    path, lease, doc = shard(tmp_path)
    lease.release()
    row = {
        "status": "deferred",
        "stage": "interpret",
        "revision": "revision",
        "identity": "identity",
        "work_key": "work",
        "deferred_reason": "scope_quarantined",
        "results": {"prepare": {"revision": "original-input"}},
    }
    prior = quarantine.scope_snapshot(path)
    proof = {
        "schema": "closer-scoped-admission/v1",
        "admitted": False,
        "callback_started": False,
        "legacy_stop_authority": False,
        "reason": "bounded denial",
        "target": "owner/held#1",
        "stage": "interpret",
        "snapshot": prior,
        "input_sha256": quarantine._digest(row["results"]),
        "binding": {
            "state_path": str(lease.state_path.resolve()),
            "pipeline_run_id": "current",
            "revision": "revision",
            "identity": "identity",
            "work_key": "work",
        },
    }
    proof_path = resources.scoped_root(path) / "scope-admissions" / "proof.json"
    write(proof_path, proof)
    row["scope_proof"] = {
        "path": str(proof_path),
        "sha256": hashlib.sha256(proof_path.read_bytes()).hexdigest(),
    }
    doc["runs"]["current"]["targets"]["owner/held#1"] = row
    write(lease.state_path, doc)
    return path, lease, doc, row, proof_path


def test_exact_scope_refusal_proof_retained_but_never_discharges_other_barriers(tmp_path):
    path, lease, doc, row, proof_path = refusal(tmp_path)
    snapshot = quarantine.scope_snapshot(path)
    assert not snapshot["quarantine_keys"] and not snapshot["global_reasons"]
    assert str(proof_path) in snapshot["source_hashes"]
    row["recovery_barrier"] = {}
    write(lease.state_path, doc)
    assert quarantine.scope_snapshot(path)["quarantine_keys"]
    row.pop("recovery_barrier")
    write(lease.state_path, doc)
    proof_path.write_bytes(proof_path.read_bytes() + b" ")
    assert quarantine.scope_snapshot(path)["quarantine_keys"]


@pytest.mark.parametrize(
    "mutation",
    [
        "none",
        "missing-run",
        "missing-target",
        "revision",
        "identity",
        "work_key",
        "stage",
        "proof",
        "results",
        "carry-extra",
    ],
)
def test_full_refusal_carry_requires_exact_still_retained_named_origin(tmp_path, mutation):
    path, lease, doc, original, proof_path = refusal(tmp_path)
    carried = deepcopy(pipeline._carry_item(original))
    assert carried["scope_proof"] == original["scope_proof"]
    doc["carry"]["owner/held#1"] = carried
    if mutation == "missing-run":
        doc["runs"].pop("current")
    elif mutation == "missing-target":
        doc["runs"]["current"]["targets"].clear()
    elif mutation == "proof":
        original["scope_proof"] = {"path": str(proof_path), "sha256": "0" * 64}
    elif mutation == "carry-extra":
        carried["invented"] = "not part of the retained projection"
    elif mutation == "results":
        original["results"] = {"prepare": {"revision": "mutated-input"}}
    elif mutation != "none":
        original[mutation] = "changed"
    write(lease.state_path, doc)
    before = {p: p.read_bytes() for p in (path, lease.state_path, proof_path)}
    snapshot = quarantine.scope_snapshot(path)
    assert not snapshot["global_reasons"]
    assert bool(snapshot["quarantine_keys"]) is (mutation != "none")
    assert {p: p.read_bytes() for p in before} == before


def test_bound_archive_chain_is_checked_and_any_byte_change_holds(tmp_path):
    path = ledger(tmp_path)
    chunk = {
        "schema": pipeline.AUDIT_ARCHIVE_SCHEMA,
        "previous_sha256": None,
        "entries": [{"schema": "closer-expired-invalid-round-recovery/v1", "targets": {}}],
    }
    raw = pipeline._canonical(chunk)
    sha = hashlib.sha256(raw).hexdigest()
    archive = pipeline._audit_archive_dir(path) / (sha + ".json")
    archive.parent.mkdir()
    archive.write_bytes(raw)
    doc = json.loads(path.read_text())
    doc["runs"]["origin"] = {
        "targets": {},
        "invalid_stop_archive": {
            "schema": pipeline.AUDIT_ARCHIVE_SCHEMA,
            "sha256": sha,
            "count": 1,
        },
    }
    write(path, doc)
    snapshot = quarantine.scope_snapshot(path)
    assert not snapshot["global_reasons"]
    assert snapshot["source_hashes"][str(archive)] == sha
    archive.write_bytes(raw + b" ")
    assert quarantine.scope_problem(snapshot, "owner/healthy#2", keys())
    assert quarantine.scope_snapshot(path)["global_reasons"]


def shard(tmp_path):
    path = ledger(tmp_path)
    root = resources.open_root(path, pipeline_schema=pipeline.SCHEMA)
    import time

    lease = resources.acquire(
        root, "owner/held#1", owner="synthetic-owner", expires_at=time.time() + 60
    )
    doc = pipeline._empty()
    doc["runs"]["current"] = {
        "created": time.time(),
        "targets": {
            "owner/held#1": {
                "status": "running",
                "stage": "act",
                "last_scheduled": time.time(),
                "scope_lease": {"target": lease.target, "owner": lease.owner, "epoch": lease.epoch},
            }
        },
    }
    write(lease.state_path, doc)
    return path, lease, doc


def test_fresh_live_shard_does_not_fence_itself_but_old_rows_and_barriers_do(tmp_path):
    path, lease, doc = shard(tmp_path)
    try:
        snapshot = quarantine.scope_snapshot(path)
        assert not snapshot["quarantine_keys"] and not snapshot["global_reasons"]
        assert quarantine.scope_snapshot(path) == snapshot
        assert quarantine.scope_problem(snapshot, "owner/held#1", keys("owner/held#1")) is None
        doc["runs"]["old"] = {
            "created": 1,
            "targets": {"owner/held#1": {"status": "uncertain", "stage": "reconcile"}},
        }
        write(lease.state_path, doc)
        snapshot = quarantine.scope_snapshot(path)
        assert "repo:owner/held" in snapshot["quarantine_keys"]
        assert quarantine.scope_problem(snapshot, "owner/healthy#2", keys()) is None
        doc["runs"].pop("old")
        doc["runs"]["current"]["targets"]["owner/held#1"]["recovery_barrier"] = {}
        write(lease.state_path, doc)
        assert quarantine.scope_snapshot(path)["quarantine_keys"]
    finally:
        lease.release()


@pytest.mark.parametrize("damage", ["state", "history", "lease", "directory"])
def test_shard_damage_known_identity_is_repo_bounded_unknown_identity_is_global(tmp_path, damage):
    path, lease, doc = shard(tmp_path)
    lease.release()
    if damage == "state":
        lease.state_path.write_text("bad JSON")
    elif damage == "history":
        doc["runs"]["current"]["invalid_stop_history"] = "broken"
        write(lease.state_path, doc)
    elif damage == "lease":
        write(lease.directory / "lease.json", {"target": "unbound"})
    else:
        lease.directory.rename(lease.directory.with_name("invalid-directory"))
    snapshot = quarantine.scope_snapshot(path)
    if damage in {"lease", "directory"}:
        assert snapshot["global_reasons"]
    else:
        assert not snapshot["global_reasons"]
        assert "repo:owner/held" in snapshot["quarantine_keys"]
        assert quarantine.scope_problem(snapshot, "owner/healthy#2", keys()) is None


def test_expired_shard_lease_does_not_discharge_running_row(tmp_path):
    path, lease, _doc = shard(tmp_path)
    try:
        value = json.loads((lease.directory / "lease.json").read_text())
        value["expires_at"] = 0
        write(lease.directory / "lease.json", value)
        assert "repo:owner/held" in quarantine.scope_snapshot(path)["quarantine_keys"]
    finally:
        lease.release()


def test_held_second_lease_missing_state_is_not_a_fresh_first_use(tmp_path):
    import time

    path, first, _doc = shard(tmp_path)
    first.release()
    second = resources.acquire(
        resources.scoped_root(path),
        "owner/held#1",
        owner="second-owner",
        expires_at=time.time() + 60,
    )
    try:
        assert second.epoch == 2
        second.state_path.unlink()
        snapshot = quarantine.scope_snapshot(path)
        assert not snapshot["global_reasons"]
        assert "repo:owner/held" in snapshot["quarantine_keys"]
        assert quarantine.scope_problem(snapshot, "owner/held#1", keys("owner/held#1"))
        assert quarantine.scope_problem(snapshot, "owner/healthy#2", keys()) is None
    finally:
        second.release()


@pytest.mark.parametrize(
    "mutation",
    [
        "old-run-current-launch",
        "old-epoch",
        "wrong-owner",
        "wrong-target",
        "missing-marker",
        "stale-launch",
        "future-launch",
        "missing-launch",
    ],
)
def test_only_exact_current_lease_and_launch_proves_current_callback(tmp_path, mutation):
    import time

    path, lease, doc = shard(tmp_path)
    try:
        row = doc["runs"]["current"]["targets"]["owner/held#1"]
        if mutation == "old-run-current-launch":
            doc["runs"]["current"]["created"] = 1
        elif mutation == "old-epoch":
            row["scope_lease"]["epoch"] = 0
        elif mutation == "wrong-owner":
            row["scope_lease"]["owner"] = "old-owner"
        elif mutation == "wrong-target":
            row["scope_lease"]["target"] = "owner/other#9"
        elif mutation == "missing-marker":
            row.pop("scope_lease")
        elif mutation == "stale-launch":
            row["last_scheduled"] = 1
        elif mutation == "future-launch":
            row["last_scheduled"] = time.time() + 60
        else:
            row.pop("last_scheduled")
        write(lease.state_path, doc)
        snapshot = quarantine.scope_snapshot(path)
        assert not snapshot["global_reasons"]
        assert bool(snapshot["quarantine_keys"]) is (mutation != "old-run-current-launch")
    finally:
        lease.release()


def test_native_remote_effect_ignores_only_inferred_foreign_repository_workspace(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    path = ledger(tmp_path, {"status": "uncertain", "stage": "act"})
    before = path.read_bytes()
    scope = quarantine.scope_snapshot(path, workspaces={"owner/held": str(shared)})
    unrelated = [*keys("owner/held#9"), "worktree:" + str(shared)]
    assert quarantine.scope_problem(scope, "owner/held#9", unrelated)
    assert (
        quarantine.scope_problem(scope, "owner/held#9", unrelated, purpose="remote_effect") is None
    )
    assert quarantine.scope_problem(
        scope,
        "owner/held#1",
        keys("owner/held#1") + ["worktree:" + str(shared)],
        purpose="remote_effect",
    )
    assert path.read_bytes() == before


@pytest.mark.parametrize("explicit", ["repo:owner/held", "worktree"])
def test_native_remote_effect_preserves_original_explicit_resource_intent(tmp_path, explicit):
    shared = tmp_path / "shared"
    shared.mkdir()
    key = "worktree:" + str(shared) if explicit == "worktree" else explicit
    path = ledger(tmp_path, {"status": "uncertain", "stage": "act", "resource_keys": [key]})
    scope = quarantine.scope_snapshot(path, workspaces={"owner/held": str(shared)})
    assert key in scope["items"][0]["explicit_original_keys"]
    assert quarantine.scope_problem(
        scope,
        "owner/held#9",
        keys("owner/held#9") + ["worktree:" + str(shared)],
        purpose="remote_effect",
    )


def test_native_remote_effect_preserves_shared_source_and_branch_aliases(tmp_path):
    row = {
        "status": "uncertain",
        "stage": "act",
        "results": {
            "prepare": {
                "snapshot": {
                    "target": "owner/held#1",
                    "sources": [{"target": "owner/linked#3"}],
                    "head_repository": "owner/held",
                    "head_branch": "shared-feature",
                }
            }
        },
    }
    path = ledger(tmp_path, row)
    scope = quarantine.scope_snapshot(path)
    base = keys("owner/held#9")
    assert quarantine.scope_problem(scope, "owner/held#9", base, purpose="remote_effect") is None
    assert quarantine.scope_problem(
        scope,
        "owner/held#9",
        base + ["issue:owner/linked#3", "repo:owner/linked"],
        purpose="remote_effect",
    )
    assert quarantine.scope_problem(
        scope, "owner/held#9", base + ["branch:owner/held@shared-feature"], purpose="remote_effect"
    )


@pytest.mark.parametrize("explicit", ["repo:owner/held", "worktree"])
def test_native_remote_effect_preserves_unresolved_history_resource_intent(tmp_path, explicit):
    shared = tmp_path / "shared"
    shared.mkdir()
    key = "worktree:" + str(shared) if explicit == "worktree" else explicit
    row = {
        "status": "uncertain",
        "stage": "act",
        "operation_history": [{"status": "unknown", "resource_keys": [key]}],
    }
    path = ledger(tmp_path, row)
    scope = quarantine.scope_snapshot(path, workspaces={"owner/held": str(shared)})
    assert key in scope["items"][0]["explicit_original_keys"]
    assert quarantine.scope_problem(
        scope,
        "owner/held#9",
        keys("owner/held#9") + ["worktree:" + str(shared)],
        purpose="remote_effect",
    )


@pytest.mark.parametrize("history", [None, {}, [None]])
def test_native_remote_effect_cannot_narrow_malformed_operation_history(tmp_path, history):
    path = ledger(tmp_path, {"status": "uncertain", "stage": "act", "operation_history": history})
    scope = quarantine.scope_snapshot(path)
    assert quarantine.scope_problem(
        scope, "owner/held#9", keys("owner/held#9"), purpose="remote_effect"
    )


@pytest.mark.parametrize(
    "binding,linked",
    [
        ({"pr_target": "owner/held#9"}, "pr:owner/held#9"),
        ({"source_targets": ["owner/source#3"]}, "issue:owner/source#3"),
        (
            {"head_repository": "owner/held", "head_branch": "old-shared"},
            "branch:owner/held@old-shared",
        ),
    ],
)
@pytest.mark.parametrize("location", ["operation", "history"])
def test_native_remote_effect_preserves_unresolved_historical_binding_aliases(
    tmp_path, binding, linked, location
):
    operation = {"status": "unknown", "binding": binding}
    row = {"status": "uncertain", "stage": "act"}
    row["operation" if location == "operation" else "operation_history"] = (
        operation if location == "operation" else [operation]
    )
    path = ledger(tmp_path, row)
    scope = quarantine.scope_snapshot(path)
    assert linked in scope["quarantine_keys"]
    aliases = keys("owner/held#9") + [linked]
    if linked.startswith("issue:"):
        aliases.append("repo:owner/source")
    assert quarantine.scope_problem(scope, "owner/held#9", aliases, purpose="remote_effect")
    assert (
        quarantine.scope_problem(
            scope, "owner/held#10", keys("owner/held#10"), purpose="remote_effect"
        )
        is None
    )


@pytest.mark.parametrize("binding", [None, [], {"source_targets": {}}, {"pr_target": None}])
def test_native_remote_effect_cannot_narrow_malformed_declared_binding(tmp_path, binding):
    path = ledger(
        tmp_path,
        {
            "status": "uncertain",
            "stage": "act",
            "operation_history": [{"status": "unknown", "binding": binding}],
        },
    )
    scope = quarantine.scope_snapshot(path)
    assert quarantine.scope_problem(
        scope, "owner/held#9", keys("owner/held#9"), purpose="remote_effect"
    )


@pytest.mark.parametrize("malformed", ["history", "binding"])
def test_malformed_intent_preserves_final_linked_repository_and_workspace_fallback(
    tmp_path, malformed
):
    shared = tmp_path / "linked-shared"
    shared.mkdir()
    row = {
        "status": "uncertain",
        "stage": "act",
        "results": {
            "prepare": {
                "snapshot": {"target": "owner/held#1", "sources": [{"target": "owner/linked#3"}]}
            }
        },
    }
    if malformed == "history":
        row["operation_history"] = None
    else:
        row["operation_history"] = [{"status": "unknown", "binding": []}]
    path = ledger(tmp_path, row)
    scope = quarantine.scope_snapshot(path, workspaces={"owner/linked": str(shared)})
    item = scope["items"][0]
    assert {"repo:owner/linked", "worktree:" + str(shared)}.issubset(item["explicit_original_keys"])
    assert quarantine.scope_problem(
        scope,
        "owner/linked#9",
        keys("owner/linked#9") + ["worktree:" + str(shared)],
        purpose="remote_effect",
    )


def test_malformed_raw_queue_history_preserves_later_claim_alias_expansion(tmp_path):
    path = ledger(tmp_path)
    raw = Path(str(path)[: -len(".pipeline.json")])
    shared = tmp_path / "later-shared"
    shared.mkdir()
    write(
        raw,
        {
            "schema": "closer-queue/v1",
            "round": {},
            "items": {
                "owner/held#1": {
                    "status": "uncertain",
                    "operation_history": None,
                    "claim_targets": ["owner/linked#3"],
                }
            },
        },
    )
    scope = quarantine.scope_snapshot(path, workspaces={"owner/linked": str(shared)})
    item = next(item for item in scope["items"] if item["run_id"] == "native_queue")
    assert item["remote_scope_narrowable"] is False
    assert "worktree:" + str(shared) in item["keys"]
    assert quarantine.scope_problem(
        scope,
        "owner/linked#9",
        keys("owner/linked#9") + ["worktree:" + str(shared)],
        purpose="remote_effect",
    )


def test_remote_inventory_does_not_require_unused_mapped_workspace(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    path = ledger(tmp_path, {"status": "uncertain", "stage": "act"})
    snapshot = quarantine.scope_snapshot(path, workspaces={"owner/held": str(shared)})
    remote_keys = keys("owner/held#9")
    assert quarantine.scope_problem(snapshot, "owner/held#9", remote_keys) == (
        "incomplete_resource_inventory"
    )
    assert (
        quarantine.scope_problem(snapshot, "owner/held#9", remote_keys, purpose="remote_effect")
        is None
    )


def test_remote_inventory_keeps_explicit_workspace_overlap(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    workspace = "worktree:" + str(shared)
    path = ledger(tmp_path)
    ad(path, workspace)
    snapshot = quarantine.scope_snapshot(path, workspaces={"owner/healthy": str(shared)})
    assert quarantine.scope_problem(
        snapshot, "owner/healthy#2", keys() + [workspace], purpose="remote_effect"
    ).startswith("quarantined_resource:")
