"""Private simulated mutation boundaries; never live acceptance or remote effects."""

from __future__ import annotations

import json

import pytest
from test_closer_queue import _run, _snapshot
from test_keepalive_supervisor import closure_fixture

import closer_queue as queue
import keepalive_supervisor as supervisor


@pytest.mark.parametrize(
    "label",
    [
        "agents:auto-pilot-pause",
        "agent:paused",
        "do-not-merge",
        "hold",
        "agents:paused",
        "needs-human",
    ],
)
def test_closure_actor_preserves_every_existing_closer_hold_label(tmp_path, label):
    _pr, issue, calls, recorded, options = closure_fixture(tmp_path)
    issue["labels"] = [{"name": label}]
    result = supervisor.close_verified_issues(**options)
    assert result["closed"] == 0
    assert issue["state"] == "open"
    assert not recorded and not any("PATCH" in call for call in calls)


@pytest.mark.parametrize("surface", ["source", "pr"])
def test_closure_actor_rechecks_labels_even_when_timestamp_does_not_change(tmp_path, surface):
    pr, issue, calls, recorded, options = closure_fixture(tmp_path)
    initial_read = options["pr_read"]
    reads = []

    def pr_read(target):
        reads.append(target)
        if len(reads) == 2:
            if surface == "source":
                issue["labels"].append({"name": "agents:auto-pilot-pause"})
            else:
                pr["labels"]["nodes"].append({"name": "do-not-merge"})
        return initial_read(target)

    options["pr_read"] = pr_read
    result = supervisor.close_verified_issues(**options)
    assert result["closed"] == 0 and reads
    assert result["skips"] == {"evidence changed before closure": 1}
    assert issue["state"] == "open" and not recorded
    assert not any("PATCH" in call or "-f" in call for call in calls)


def test_initial_pr_hold_also_refuses_source_closure(tmp_path):
    pr, issue, calls, recorded, options = closure_fixture(tmp_path)
    pr["labels"]["nodes"].append({"name": "hold"})
    result = supervisor.close_verified_issues(**options)
    assert result["closed"] == 0 and issue["state"] == "open"
    assert not recorded and not any("PATCH" in call for call in calls)


@pytest.mark.parametrize("boundary", ["before_mutation", "after_mutation"])
def test_uncertain_external_boundary_reconciles_before_any_replay(tmp_path, boundary):
    path = tmp_path / "simulated-actions.json"
    target = "offline/actions#1"
    state = {"applied": False, "attempts": 0, "mutations": 0, "reconciliations": 0}

    def observe(_target):
        return _snapshot(
            "captured-revision",
            "complete" if state["applied"] else "merge",
            eligible=not state["applied"],
        )

    def execute(*_args):
        state["attempts"] += 1
        if boundary == "before_mutation" and state["attempts"] == 1:
            raise TimeoutError("simulated transport failed before remote mutation")
        assert not state["applied"], "a confirmed mutation must never be repeated"
        state["applied"] = True
        state["mutations"] += 1
        if boundary == "after_mutation":
            raise TimeoutError("simulated remote mutation succeeded; response was lost")
        return {"status": "completed", "advanced": True}

    def reconcile(*_args):
        state["reconciliations"] += 1
        return {
            "status": "completed" if state["applied"] else "confirmed_not_started",
            "target_complete": state["applied"],
        }

    _run(path, [target], observe, execute, reconcile=reconcile)
    first = json.loads(path.read_text())["items"][target]
    assert first["operation"]["status"] == "uncertain"
    assert state["attempts"] == 1
    _run(path, [target], observe, execute, reconcile=reconcile)
    assert state["reconciliations"] >= 1
    assert state["mutations"] == 1
    assert state["attempts"] == (2 if boundary == "before_mutation" else 1)
    _run(path, [target], observe, execute, reconcile=reconcile)
    assert state["mutations"] == 1
    assert queue._read_state(path)["items"][target]["operation"]["status"] == "completed"


@pytest.mark.parametrize("write_boundary", ["intent", "after_external_mutation"])
def test_ledger_write_failure_preserves_safe_restart_boundary(
    tmp_path, monkeypatch, write_boundary
):
    path = tmp_path / "simulated-write-failure.json"
    target = "offline/actions#2"
    remote = {"applied": False, "calls": 0}
    failed = []
    original_write = queue._atomic_write

    def write(destination, doc):
        operation = doc.get("items", {}).get(target, {}).get("operation") or {}
        wanted = "submitted" if write_boundary == "intent" else "completed"
        if operation.get("status") == wanted and not failed:
            failed.append(wanted)
            raise OSError("simulated ledger filesystem write failure")
        return original_write(destination, doc)

    def observe(_target):
        return _snapshot(
            "captured-revision",
            "complete" if remote["applied"] else "merge",
            eligible=not remote["applied"],
        )

    def execute(*_args):
        assert not remote["applied"]
        remote["calls"] += 1
        remote["applied"] = True
        return {"status": "completed", "advanced": True}

    reconciled = []

    def reconcile(_target, operation):
        reconciled.append(operation["id"])
        return {
            "status": "completed" if remote["applied"] else "confirmed_not_started",
            "target_complete": remote["applied"],
        }

    monkeypatch.setattr(queue, "_atomic_write", write)
    with pytest.raises(OSError, match="simulated ledger"):
        _run(path, [target], observe, execute, reconcile=reconcile)
    assert failed
    assert remote["calls"] == (0 if write_boundary == "intent" else 1)
    saved = queue._read_state(path)["items"][target].get("operation")
    if write_boundary == "after_external_mutation":
        assert saved["status"] == "uncertain"
        assert saved["error_type"] == "OSError"
        assert saved["next_trigger"] == "reconcile_operation_before_retry"
    monkeypatch.setattr(queue, "_atomic_write", original_write)
    _run(path, [target], observe, execute, reconcile=reconcile)
    assert remote["calls"] == 1
    assert queue._read_state(path)["items"][target]["operation"]["status"] == "completed"
    if write_boundary == "after_external_mutation":
        assert reconciled == [
            saved["id"]
        ], "reconcile the original effect before considering replay"
    _run(path, [target], observe, execute, reconcile=reconcile)
    assert remote["calls"] == 1
