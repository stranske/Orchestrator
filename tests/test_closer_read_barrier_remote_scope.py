"""An authenticated terminal read wait changes only remote admission, never history."""

import json
from copy import deepcopy

import pytest
from test_closer_native_admission_classification import blocked_fixture
from test_closer_quarantine_closure import write

import closer_pipeline as pipeline
import closer_quarantine as quarantine
import closer_resources as resources


def read_barrier_fixture(tmp_path, monkeypatch, *, compact=False):
    path, inner, row, doc = blocked_fixture(tmp_path, monkeypatch)
    target = next(iter(doc["runs"]["original"]["targets"]))
    act = row["results"]["act"]
    if compact:
        receipt = act["queue_receipt"]
        snapshot = row["results"]["prepare"]["snapshot"]
        proof = {
            "schema": pipeline.INNER_WAIT_SCHEMA,
            "target": target,
            "revision": row["revision"],
            "head": snapshot["head"],
            "reason": act["reason"],
            "next_trigger": act["next_trigger"],
            "inner_path": str(inner),
            "receipt_sha256": pipeline._digest(receipt),
            "acceptance_authority": False,
        }
        act = {
            "status": "deferred",
            "reason": "validated_inner_wait",
            "next_trigger": act["next_trigger"],
            "queue_receipt": receipt,
            "acceptance_authority": False,
            "resource_evidence": {"inner_wait": proof},
        }
        row["results"]["act"] = act
    row.update(
        status="failed",
        stage="interpret",
        error=pipeline.READ_BARRIER_ERROR,
        deferred_reason="deadline_exhausted",
        deferred_next_trigger=pipeline.READ_BARRIER_TRIGGER,
        resource_evidence={"type": "active_deadline", "observed_at": 10, "deadline": 20},
    )
    origin = {
        key: deepcopy(row.get(key))
        for key in (
            "stage",
            "revision",
            "identity",
            "work_key",
            "deferred_reason",
            "pending_kind",
            "pending_evidence",
            "resource_evidence",
        )
    }
    origin.update(
        status="deferred",
        recovery="quarantine_read",
        item_sha256=pipeline._digest(row),
        act_result=deepcopy(act),
    )
    row["recovery_barrier"] = {
        "schema": pipeline.READ_BARRIER_SCHEMA,
        "source_run_id": "original-read",
        "item_sha256": origin["item_sha256"],
        "act_result_sha256": pipeline._digest(act),
        **{key: row[key] for key in ("revision", "identity", "work_key")},
    }
    doc["runs"]["original"]["invalid_stop_history"] = [
        {
            "schema": "closer-expired-invalid-round-recovery/v1",
            "source_run_id": "original-read",
            "targets": {target: origin},
        }
    ]
    write(path, doc)
    assert pipeline._valid_read_recovery_barrier(
        target, row, doc["runs"]["original"]["invalid_stop_history"]
    )
    return path, inner, row, doc, target


@pytest.mark.parametrize("compact", [False, True])
def test_validated_read_wait_does_not_claim_remote_operation(tmp_path, monkeypatch, compact):
    path, inner, row, _doc, target = read_barrier_fixture(tmp_path, monkeypatch, compact=compact)
    before = (path.read_bytes(), inner.read_bytes())
    scope = quarantine.scope_snapshot(path)
    assert scope["quarantine_keys"]  # The historical barrier remains present.
    remote = ["pr:" + target, "repo:owner/repo", "issue:owner/repo#1"]
    assert quarantine.scope_problem(scope, target, remote, purpose="remote_effect") is None
    assert scope["items"][0]["remote_terminal_read_wait"] is True
    assert quarantine.scope_problem(scope, target, remote, purpose="effect")
    assert row["recovery_barrier"]["schema"] == pipeline.READ_BARRIER_SCHEMA
    assert (path.read_bytes(), inner.read_bytes()) == before


def reseal(row, doc, target):
    """Keep the audit authentic when isolating a different failed proof requirement."""
    act = row["results"]["act"]
    row["recovery_barrier"]["act_result_sha256"] = pipeline._digest(act)
    doc["runs"]["original"]["invalid_stop_history"][0]["targets"][target]["act_result"] = deepcopy(
        act
    )


@pytest.mark.parametrize(
    "change",
    [
        "missing_receipt",
        "invalid_receipt",
        "missing_queue",
        "operation",
        "history",
        "revision",
        "head",
        "barrier_hash",
        "audit",
        "snapshot",
        "provider_unknown",
        "outer_operation",
        "outer_history",
        "act_barrier",
        "progress",
    ],
)
def test_incomplete_or_consequential_history_keeps_remote_fence(tmp_path, monkeypatch, change):
    path, inner, row, doc, target = read_barrier_fixture(tmp_path, monkeypatch)
    native = json.loads(inner.read_text())
    if change == "missing_receipt":
        row["results"]["act"].pop("queue_receipt")
        reseal(row, doc, target)
    elif change == "invalid_receipt":
        row["results"]["act"]["queue_receipt"]["valid"] = False
        reseal(row, doc, target)
    elif change == "missing_queue":
        inner.unlink()
    elif change == "operation":
        native["items"][target]["operation"] = {"action": "merge", "status": "running"}
    elif change == "history":
        native["items"][target]["operation_history"] = [{"action": "merge", "status": "uncertain"}]
    elif change == "revision":
        native["round"]["source_revisions"][target] = "other"
    elif change == "head":
        native["items"][target]["evidence"]["head"] = "f" * 40
    elif change == "barrier_hash":
        row["recovery_barrier"]["act_result_sha256"] = "f" * 64
    elif change == "audit":
        doc["runs"]["original"]["invalid_stop_history"] = []
    elif change == "snapshot":
        row["results"]["prepare"]["snapshot_ref"]["sha256"] = "f" * 64
    elif change == "provider_unknown":
        row["provider_outcome_uncertain"] = {"run_id": "pending"}
    elif change == "outer_operation":
        row["operation"] = {"action": "merge", "status": "uncertain"}
    elif change == "outer_history":
        row["operation_history"] = [{"action": "merge", "status": "uncertain"}]
    elif change == "act_barrier":
        row["recovery_barrier"]["schema"] = "closer-unverified-act-barrier/v1"
    else:
        native["round"]["receipt"]["progress"] = [{"action": "merge"}]
    if change != "missing_queue":
        write(inner, native)
    write(path, doc)
    before = path.read_bytes()
    scope = quarantine.scope_snapshot(path)
    assert not any(item["remote_terminal_read_wait"] for item in scope["items"])
    assert quarantine.scope_problem(
        scope, target, ["pr:" + target, "repo:owner/repo"], purpose="remote_effect"
    )
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "declared",
    [
        "issue:owner/repo#7",
        "pr:owner/repo#7",
        "branch:owner/repo@pinned",
        "repo:owner/repo",
    ],
)
def test_declared_remote_intent_remains_even_with_valid_wait(tmp_path, monkeypatch, declared):
    path, _inner, row, doc, target = read_barrier_fixture(tmp_path, monkeypatch)
    row["resource_keys"] = [declared]
    write(path, doc)
    scope = quarantine.scope_snapshot(path)
    assert scope["items"][0]["remote_terminal_read_wait"] is True
    baseline = quarantine.scope_problem(
        scope, target, ["pr:" + target, "repo:owner/repo"], purpose="remote_effect"
    )
    assert bool(baseline) is (declared == "repo:owner/repo")
    assert quarantine.scope_problem(
        scope,
        target,
        ["pr:" + target, "repo:owner/repo", declared],
        purpose="remote_effect",
    ).startswith("quarantined_resource:")


@pytest.mark.parametrize("declared", [False, True])
def test_local_workspace_stays_quarantined_for_both_effect_paths(tmp_path, monkeypatch, declared):
    path, _inner, row, doc, target = read_barrier_fixture(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    local_key = "worktree:" + str(workspace)
    if declared:
        row["resource_keys"] = [local_key]
        write(path, doc)
    scope = quarantine.scope_snapshot(path, workspaces={"owner/repo": str(workspace)})
    assert scope["items"][0]["remote_terminal_read_wait"] is True
    remote = ["pr:" + target, "repo:owner/repo"]
    assert quarantine.scope_problem(scope, target, remote, purpose="remote_effect") is None
    for purpose in ("effect", "remote_effect"):
        assert quarantine.scope_problem(scope, target, [*remote, local_key], purpose=purpose)


def test_independent_live_reservation_is_not_hidden_by_wait_projection(tmp_path, monkeypatch):
    path, _inner, _row, _doc, target = read_barrier_fixture(tmp_path, monkeypatch)
    key = "pr:" + target
    advertisement = resources._key_dir(resources.scoped_root(path), key) / "legacy-intent.json"
    write(advertisement, {"key": key, "target": target, "owner": "different-live-owner"})
    before = advertisement.read_bytes()
    scope = quarantine.scope_snapshot(path)
    assert quarantine.scope_problem(
        scope, target, [key, "repo:owner/repo"], purpose="remote_effect"
    )
    assert advertisement.read_bytes() == before


def test_other_real_uncertain_operation_stays_blocked(tmp_path, monkeypatch):
    path, _inner, _row, doc, target = read_barrier_fixture(tmp_path, monkeypatch)
    doc["runs"]["real-unknown"] = {
        "targets": {
            target: {
                "status": "uncertain",
                "stage": "act",
                "operation": {
                    "action": "merge",
                    "status": "uncertain",
                    "resource_keys": ["pr:" + target],
                },
            }
        }
    }
    write(path, doc)
    before = path.read_bytes()
    scope = quarantine.scope_snapshot(path)
    assert quarantine.scope_problem(
        scope, target, ["pr:" + target, "repo:owner/repo"], purpose="remote_effect"
    )
    assert path.read_bytes() == before


def test_census_proof_is_invalidated_by_later_queue_change(tmp_path, monkeypatch):
    path, inner, _row, _doc, target = read_barrier_fixture(tmp_path, monkeypatch)
    scope = quarantine.scope_snapshot(path)
    assert str(inner) in scope["source_hashes"]
    native = json.loads(inner.read_text())
    native["items"][target]["operation"] = {"action": "merge", "status": "uncertain"}
    write(inner, native)
    assert (
        quarantine.scope_problem(
            scope, target, ["pr:" + target, "repo:owner/repo"], purpose="remote_effect"
        )
        == "scope_sources_changed"
    )


def test_archived_audit_is_bound_and_kept_in_source_inventory(tmp_path, monkeypatch):
    import hashlib

    path, _inner, _row, doc, target = read_barrier_fixture(tmp_path, monkeypatch)
    origin = doc["runs"]["original"]
    actual = origin["invalid_stop_history"][0]
    chunk = {"schema": pipeline.AUDIT_ARCHIVE_SCHEMA, "previous_sha256": None, "entries": [actual]}
    raw = pipeline._canonical(chunk)
    sha = hashlib.sha256(raw).hexdigest()
    archive = pipeline._audit_archive_dir(path) / (sha + ".json")
    archive.parent.mkdir()
    archive.write_bytes(raw)
    origin["invalid_stop_archive"] = {
        "schema": pipeline.AUDIT_ARCHIVE_SCHEMA,
        "sha256": sha,
        "count": 1,
    }
    origin["invalid_stop_history"] = [
        {"schema": "closer-expired-invalid-round-recovery/v1", "targets": {}}
    ]
    write(path, doc)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    scope = quarantine.scope_snapshot(path)
    assert scope["items"][0]["remote_terminal_read_wait"] is True
    assert str(archive) in scope["source_hashes"]
    assert (
        quarantine.scope_problem(
            scope, target, ["pr:" + target, "repo:owner/repo"], purpose="remote_effect"
        )
        is None
    )
    assert {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    archive.write_bytes(raw + b" ")
    assert (
        quarantine.scope_problem(
            scope, target, ["pr:" + target, "repo:owner/repo"], purpose="remote_effect"
        )
        == "scope_sources_changed"
    )
    assert not any(
        item["remote_terminal_read_wait"] for item in quarantine.scope_snapshot(path)["items"]
    )


def test_shared_ledger_is_not_reparsed_for_each_wait(tmp_path, monkeypatch):
    path, _inner, _row, doc, target = read_barrier_fixture(tmp_path, monkeypatch)
    doc["runs"]["another-retained-read"] = deepcopy(doc["runs"]["original"])
    write(path, doc)
    original = quarantine._Census.read
    reads = []

    def counted(self, source):
        if source == path:
            reads.append(source)
        return original(self, source)

    monkeypatch.setattr(quarantine._Census, "read", counted)
    scope = quarantine.scope_snapshot(path)
    assert len(reads) == 1
    assert len(scope["items"]) == 2
    assert all(item["remote_terminal_read_wait"] for item in scope["items"])
    assert (
        quarantine.scope_problem(
            scope, target, ["pr:" + target, "repo:owner/repo"], purpose="remote_effect"
        )
        is None
    )


def test_shared_run_resolves_audit_once_without_reusing_target_verdict(tmp_path, monkeypatch):
    path, _inner, row, doc, target = read_barrier_fixture(tmp_path, monkeypatch)
    other = "owner/repo#9"
    # Two real same-run census rows; the second cannot borrow the first's audit,
    # even though both use the same expensive immutable archive resolution.
    doc["runs"]["original"]["targets"][other] = deepcopy(row)
    write(path, doc)
    calls = []
    original = pipeline._resolved_audit_history

    def resolved(source, run):
        calls.append((source, run))
        return original(source, run)

    monkeypatch.setattr(pipeline, "_resolved_audit_history", resolved)
    scope = quarantine.scope_snapshot(path)
    assert len(calls) == 1
    by_target = {item["target"]: item for item in scope["items"]}
    assert by_target[target]["remote_terminal_read_wait"] is True
    assert by_target[other]["remote_terminal_read_wait"] is False
    # Audit acquisition failures are also shared only within one census and
    # remain fail-closed for every target; a fresh census may reacquire later.
    calls.clear()

    def failed(source, run):
        calls.append((source, run))
        raise ValueError("unavailable audit archive")

    monkeypatch.setattr(pipeline, "_resolved_audit_history", failed)
    rejected = quarantine.scope_snapshot(path)
    assert len(calls) == 1
    assert not any(item["remote_terminal_read_wait"] for item in rejected["items"])
