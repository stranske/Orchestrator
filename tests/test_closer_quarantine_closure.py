"""Retained producer evidence narrows conflict domains without clearing outcomes."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

import closer_change_feed as feed
import closer_evidence as evidence
import closer_pipeline as pipeline
import closer_quarantine as quarantine


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def prepared(tmp_path, extra_sources=()):
    raw = tmp_path / "dispatch.json"
    path = Path(str(raw) + ".pipeline.json")
    target = "owner/repo#1"
    snapshot = {
        "target": target,
        "pr_target": "owner/repo#2",
        "kind": "pr",
        "state": "OPEN",
        "revision": "r" * 64,
        "head": "a" * 40,
        "base": "b" * 40,
        "merge": None,
        "head_repository": "owner/repo",
        "head_branch": "feature-one",
        "review_revision": "review",
        "sources": [
            {
                "target": target,
                "body": "## Tasks\n- [ ] Preserve exact criteria",
                "state": "open",
                "labels": [],
            }
        ],
        "threads": [],
        "checks": "SUCCESS",
    }
    root = Path(str(raw) + ".evidence")
    snapshot["sources"].extend(
        {
            "target": target,
            "body": "## Tasks\n- [ ] Preserve linked criteria",
            "state": "open",
            "labels": [],
        }
        for target in extra_sources
    )

    def api(endpoint, **_kwargs):
        if "/compare/" in endpoint:
            return {
                "files": [
                    {
                        "filename": "code.py",
                        "sha": "c" * 40,
                        "status": "modified",
                        "patch": "@@ -1 +1 @@\n-old\n+new",
                    }
                ]
            }
        if "/actions/runs?" in endpoint:
            return {"workflow_runs": []}
        raise AssertionError(endpoint)

    packet = evidence.collect_evidence(snapshot, api, root)
    manifest = root / "manifests" / (packet["manifest"]["packet_sha256"] + ".json")
    packet_ref = {
        "path": str(manifest),
        "sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
    }
    encoded = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    sha = hashlib.sha256(encoded).hexdigest()
    retained = root / "snapshots" / (sha + ".json")
    retained.parent.mkdir()
    retained.write_bytes(encoded)
    row = {
        "status": "failed",
        "stage": "interpret",
        "revision": snapshot["revision"],
        "identity": "original-identity",
        "work_key": "original-work",
        "recovery_barrier": {"revision": snapshot["revision"], "identity": "original-identity"},
        "results": {
            "prepare": {
                "snapshot_ref": {"path": str(retained), "sha256": sha},
                "packet_ref": packet_ref,
            }
        },
    }
    doc = pipeline._empty()
    doc["runs"]["original"] = {"targets": {target: row}}
    write(path, doc)
    return path, raw, snapshot, row, doc, packet_ref, retained


def inventory(target="owner/repo#9", branch="other-feature", **kwargs):
    result = ["pr:" + target, "repo:owner/repo", "branch:owner/repo@" + branch]
    result.extend(kwargs.get("aliases", []))
    return result


def test_exact_prepared_closure_allows_same_repo_disjoint_but_holds_every_actual_alias(tmp_path):
    path, _raw, _snapshot, row, _doc, _ref, retained = prepared(tmp_path)
    original = path.read_bytes()
    workspace = str(tmp_path / "original-checkout")
    other_workspace = str(tmp_path / "separate-checkout")
    snapshot = quarantine.scope_snapshot(
        path, workspaces={"owner/repo#1": workspace, "owner/repo#9": other_workspace}
    )
    assert not snapshot["global_reasons"]
    assert "repo:owner/repo" not in snapshot["quarantine_keys"]
    assert {"pr:owner/repo#2", "issue:owner/repo#1", "branch:owner/repo@feature-one"}.issubset(
        snapshot["quarantine_keys"]
    )
    assert (
        quarantine.scope_problem(
            snapshot, "owner/repo#9", inventory(aliases=["worktree:" + other_workspace])
        )
        is None
    )
    for alias in ("pr:owner/repo#2", "issue:owner/repo#1", "branch:owner/repo@feature-one"):
        assert quarantine.scope_problem(
            snapshot, "owner/repo#9", inventory(aliases=[alias, "worktree:" + other_workspace])
        )
    assert snapshot["items"][0]["closure"] == "complete_immutable_snapshot"
    assert (
        snapshot["items"][0]["closure_proof"]["snapshot_ref"]["sha256"]
        == hashlib.sha256(retained.read_bytes()).hexdigest()
    )
    assert snapshot["items"][0]["item_sha256"] == quarantine._digest(row)
    assert path.read_bytes() == original


def test_complete_read_scope_keeps_actual_authorized_worktree_collision(tmp_path):
    path, *_ = prepared(tmp_path)
    workspace = str(tmp_path / "checkout")
    snapshot = quarantine.scope_snapshot(path, workspaces={"owner/repo": workspace})
    assert "worktree:" + workspace in snapshot["quarantine_keys"]
    assert quarantine.scope_problem(
        snapshot, "owner/repo#9", inventory(aliases=["worktree:" + workspace])
    )


@pytest.mark.parametrize("damage", ["snapshot", "missing", "head", "revision", "packet", "sources"])
def test_missing_or_mismatched_closure_keeps_repository_fallback(tmp_path, damage):
    path, _raw, _snap, row, doc, _ref, retained = prepared(tmp_path)
    if damage == "snapshot":
        retained.write_bytes(retained.read_bytes() + b" ")
    elif damage == "missing":
        retained.unlink()
    elif damage == "revision":
        row["revision"] = "changed"
    elif damage == "packet":
        row["results"]["prepare"]["packet_ref"]["sha256"] = "0" * 64
    else:
        value = json.loads(retained.read_bytes())
        value["head" if damage == "head" else "sources"] = "wrong" if damage == "head" else []
        retained.write_text(json.dumps(value))
    write(path, doc)
    snapshot = quarantine.scope_snapshot(path)
    assert "repo:owner/repo" in snapshot["quarantine_keys"]
    assert (
        row["recovery_barrier"]
        and json.loads(path.read_text())["runs"]["original"]["targets"]["owner/repo#1"] == row
    )


def test_genuine_record_snapshot_only_is_observation_not_effect_and_preserves_intents(tmp_path):
    path, raw, snap, _row, doc, packet_ref, _retained = prepared(tmp_path)
    doc["runs"] = {}
    write(path, doc)
    feed.record_snapshot(raw, snap["target"], snap, packet_ref=packet_ref)
    before = raw.read_bytes()
    result = quarantine.scope_snapshot(path)
    assert not result["quarantine_keys"] and not result["global_reasons"]
    assert len(result["observations"]) == 1
    assert (
        result["observations"][0]["producer_contract"]
        == "record_snapshot_preserves_operation_and_history"
    )
    assert quarantine.scope_problem(result, snap["target"], inventory(snap["target"])) is None
    assert raw.read_bytes() == before
    state = json.loads(before)
    operation = {
        "status": "uncertain",
        "action": "merge",
        "binding": {
            "pr_target": "owner/repo#2",
            "head": snap["head"],
            "merge": None,
            "source_targets": [snap["target"]],
        },
    }
    state["items"][snap["target"]].update(
        operation=operation, operation_history=[deepcopy(operation)]
    )
    write(raw, state)
    feed.record_snapshot(raw, snap["target"], snap, packet_ref=packet_ref)
    actual = json.loads(raw.read_text())["items"][snap["target"]]
    assert actual["operation"] == operation and actual["operation_history"] == [operation]
    held = quarantine.scope_snapshot(path)
    assert not held["observations"] and held["quarantine_keys"]


def test_exact_bound_remote_github_unknown_does_not_invent_filesystem_scope(tmp_path):
    path, raw, snap, _row, doc, packet_ref, _retained = prepared(tmp_path)
    doc["runs"] = {}
    write(path, doc)
    feed.record_snapshot(raw, snap["target"], snap, packet_ref=packet_ref)
    state = json.loads(raw.read_text())
    state["items"][snap["target"]].update(
        status="deferred",
        action="merge",
        operation_history=[],
        operation={
            "status": "uncertain",
            "action": "merge",
            "binding": {
                "pr_target": snap["pr_target"],
                "head": snap["head"],
                "merge": None,
                "source_targets": [snap["target"]],
            },
        },
    )
    write(raw, state)
    snapshot = quarantine.scope_snapshot(path, workspaces={"owner/repo": str(tmp_path / "shared")})
    assert "repo:owner/repo" not in snapshot["quarantine_keys"]
    assert not any(key.startswith("worktree:") for key in snapshot["quarantine_keys"])
    assert (
        snapshot["items"][0]["closure_proof"]["action_contract"] == "bound_remote_github_operation"
    )
    assert (
        quarantine.scope_problem(
            snapshot, "owner/repo#9", inventory(aliases=["worktree:" + str(tmp_path / "shared")])
        )
        is None
    )
    state["items"][snap["target"]]["operation"]["binding"]["source_targets"] = ["owner/other#7"]
    write(raw, state)
    assert "repo:owner/repo" in quarantine.scope_snapshot(path)["quarantine_keys"]


def test_issue_only_unproved_exception_still_keeps_repository_boundary(tmp_path):
    path = tmp_path / "dispatch.json.pipeline.json"
    doc = pipeline._empty()
    doc["runs"]["original"] = {
        "targets": {
            "owner/repo#1": {"status": "queued", "stage": "exception", "revision": "unbound"}
        }
    }
    write(path, doc)
    snapshot = quarantine.scope_snapshot(path)
    assert "repo:owner/repo" in snapshot["quarantine_keys"]


def test_unique_read_budget_does_not_charge_duplicate_immutable_refs_but_detects_tampering(
    tmp_path, monkeypatch
):
    path = tmp_path / "retained.json"
    write(path, {"immutable": True})
    raw = path.read_bytes()
    monkeypatch.setattr(quarantine, "MAX_BYTES", len(raw) + 1)
    census = quarantine._Census(tmp_path / "pipeline.json")
    for _ in range(20):
        assert census.read(path) == {"immutable": True}
    assert census.bytes == len(raw)
    path.write_bytes(raw + b" ")
    with pytest.raises(ValueError, match="source_changed_during_census"):
        census.read(path)
    assert census.reasons


@pytest.mark.parametrize(
    "extra",
    [
        {"operation": None},
        {"operation_history": []},
        {"status": "observed"},
        {"provider_outcome_uncertain": {"unknown": True}},
    ],
)
def test_extra_native_or_unknown_fields_cannot_borrow_observation_contract(tmp_path, extra):
    path, raw, snap, _row, doc, packet_ref, _retained = prepared(tmp_path)
    doc["runs"] = {}
    write(path, doc)
    feed.record_snapshot(raw, snap["target"], snap, packet_ref=packet_ref)
    value = json.loads(raw.read_text())
    value["items"][snap["target"]].update(extra)
    write(raw, value)
    snapshot = quarantine.scope_snapshot(path)
    assert not snapshot["observations"] and snapshot["quarantine_keys"]


def test_original_cross_repository_sources_are_in_complete_closure(tmp_path):
    path, *_ = prepared(tmp_path, extra_sources=["owner/linked#7"])
    snapshot = quarantine.scope_snapshot(
        path, workspaces={"owner/repo": str(tmp_path / "checkout")}
    )
    assert "issue:owner/linked#7" in snapshot["quarantine_keys"]
    assert "repo:owner/linked" not in snapshot["quarantine_keys"]
    assert (
        quarantine.scope_problem(
            snapshot, "owner/linked#8", ["pr:owner/linked#8", "repo:owner/linked"]
        )
        is None
    )
    assert quarantine.scope_problem(
        snapshot,
        "owner/linked#8",
        ["pr:owner/linked#8", "repo:owner/linked", "issue:owner/linked#7"],
    )


def test_real_source_only_record_snapshot_is_still_positive_observation(tmp_path):
    raw = tmp_path / "dispatch.json"
    path = Path(str(raw) + ".pipeline.json")
    write(path, pipeline._empty())
    source = {
        "target": "owner/repo#1",
        "body": "## Tasks\n- [ ] Preserve original criteria",
        "state": "open",
        "labels": [],
    }
    snap = {
        "target": source["target"],
        "kind": "issue",
        "state": "OPEN",
        "revision": "original-source",
        "sources": [source],
    }
    root = Path(str(raw) + ".evidence")
    packet = evidence.collect_evidence(
        snap,
        lambda *_a, **_kw: pytest.fail("source-only collection must not query PR operations"),
        root,
    )
    manifest = root / "manifests" / (packet["manifest"]["packet_sha256"] + ".json")
    packet_ref = {
        "path": str(manifest),
        "sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
    }
    data = json.dumps(snap, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    sha = hashlib.sha256(data).hexdigest()
    retained = root / "snapshots" / (sha + ".json")
    retained.parent.mkdir()
    retained.write_bytes(data)
    feed.record_snapshot(raw, source["target"], snap, packet_ref=packet_ref)
    before = raw.read_bytes()
    result = quarantine.scope_snapshot(path)
    assert not result["quarantine_keys"] and not result["global_reasons"]
    assert len(result["observations"]) == 1 and raw.read_bytes() == before


@pytest.mark.parametrize(
    "checkout,target,remote,accepted",
    [
        ("Example", "owner/example#1", "https://github.com/Owner/Example.git", True),
        ("Example", "OWNER/EXAMPLE#1", "git@github.com:owner/example.git", True),
        ("Workflows-steward", "owner/workflows#1", "https://github.com/Owner/Workflows.git", True),
        ("Example", "owner/example#1", "https://github.com.invalid/Owner/Example.git", False),
        ("Example", "owner/example#1", "https://gitlab.com/Owner/Example.git", False),
        ("Example", "owner/example#1", "https://github.com/Other/Example.git", False),
    ],
)
def test_known_workspaces_casefold_identity_preserves_host_and_physical_path(
    tmp_path, monkeypatch, checkout, target, remote, accepted
):
    from types import SimpleNamespace

    import closer_dispatcher as dispatcher

    candidate = tmp_path / checkout
    candidate.mkdir()
    monkeypatch.setenv("ORCH_FLEET_ROOT", str(tmp_path))
    calls = []

    def remote_read(argv, **kwargs):
        calls.append(argv)
        assert argv == ["git", "-C", str(candidate), "remote", "get-url", "origin"]
        return SimpleNamespace(returncode=0, stdout=remote + "\n")

    monkeypatch.setattr(dispatcher.subprocess, "run", remote_read)
    result = dispatcher.known_workspaces([target])
    assert result == ({target.split("#")[0]: str(candidate.resolve())} if accepted else {})
    assert len(calls) == 1


@pytest.mark.parametrize("branch", [None, 7, "", " feature "])
def test_complete_closure_requires_original_branch_identity(tmp_path, monkeypatch, branch):
    path, _raw, snap, row, *_ = prepared(tmp_path)
    snap["head_branch"] = branch
    census = quarantine._Census(path)
    monkeypatch.setattr(census, "verified_snapshot", lambda *_args: (snap, {}))
    with pytest.raises(ValueError, match="branch_scope_incomplete"):
        census.exact_closure(snap["target"], row)


@pytest.mark.parametrize(
    "native_fields",
    [
        {"status": "uncertain", "action": "merge", "operation": None},
        {"status": "uncertain", "action": "merge"},
        {"status": "uncertain", "action": "merge", "operation": "unreadable"},
        {"status": "deferred", "action": "wait", "eligible": True, "operation": None},
        {
            "status": "complete",
            "operation": None,
            "operation_history": [{"status": "uncertain", "action": "merge"}],
        },
        {
            "status": "uncertain",
            "operation_history": [{"status": "uncertain", "action": "close_source"}],
            "claim_targets": ["owner/linked#7"],
            "repo_keys": ["owner/linked"],
        },
    ],
)
def test_unsafe_raw_queue_without_operation_intent_keeps_repository_fence(tmp_path, native_fields):
    path, raw, snap, _row, doc, packet_ref, _retained = prepared(tmp_path)
    doc["runs"] = {}
    write(path, doc)
    feed.record_snapshot(raw, snap["target"], snap, packet_ref=packet_ref)
    state = json.loads(raw.read_text())
    state["items"][snap["target"]].update(native_fields)
    write(raw, state)
    original = raw.read_bytes()
    snapshot = quarantine.scope_snapshot(path)  # No workspace mapping may hide the defect.
    assert "repo:owner/repo" in snapshot["quarantine_keys"]
    assert not snapshot["observations"] and not snapshot["global_reasons"]
    row = snapshot["items"][0]
    assert row["closure"] == "repository_fallback"
    assert row["closure_reason"] == "original_action_scope_unavailable"
    assert quarantine.scope_problem(snapshot, "owner/repo#9", inventory())
    assert (
        quarantine.scope_problem(
            snapshot, "owner/other#9", ["pr:owner/other#9", "repo:owner/other"]
        )
        is None
    )
    if native_fields.get("claim_targets"):
        assert "issue:owner/linked#7" in snapshot["quarantine_keys"]
        assert "repo:owner/linked" in snapshot["quarantine_keys"]
    assert raw.read_bytes() == original


@pytest.mark.parametrize("workspace_mode", ["missing", "other_target_only", "ambiguous"])
def test_unsettled_local_scope_needs_unique_verified_execution_workspace(tmp_path, workspace_mode):
    path, *_ = prepared(tmp_path)
    mapping = {}
    if workspace_mode == "other_target_only":
        mapping = {"owner/repo#9": str(tmp_path / "other-checkout")}
    elif workspace_mode == "ambiguous":
        mapping = {"owner/repo#1": str(tmp_path / "one"), "owner/repo": str(tmp_path / "two")}
    snapshot = quarantine.scope_snapshot(path, workspaces=mapping)
    row = snapshot["items"][0]
    assert row["closure_reason"] == "original_execution_workspace_unavailable"
    assert "repo:owner/repo" in row["keys"]
    candidate = inventory(aliases=["worktree:" + str(tmp_path / "other-checkout")])
    if workspace_mode == "ambiguous":
        candidate = inventory(aliases=["worktree:" + str(tmp_path / "two")])
    assert quarantine.scope_problem(snapshot, "owner/repo#9", candidate)
