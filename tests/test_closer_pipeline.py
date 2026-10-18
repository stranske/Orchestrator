from __future__ import annotations

import hashlib
import json
import threading
import time

import pytest

import closer_pipeline
import closer_queue
from closer_pipeline import run_pipeline, validate_stop


class FakeRoundDeadlineExhausted(TimeoutError):
    def __init__(self, deadline: float, remaining_seconds: float):
        super().__init__("round budget too close to launch read")
        self.deadline = deadline
        self.remaining_seconds = remaining_seconds


class FakeRoundCapacityDeferred(RuntimeError):
    def __init__(self, *, resource: str, throttle: dict, observed_at: float, reset_at_epoch: float):
        super().__init__("GitHub capacity is shed")
        self.resource = resource
        self.throttle = throttle
        self.observed_at = observed_at
        self.reset_at_epoch = reset_at_epoch


def callbacks(*, interpret=None, act=None, exception=None):
    def semantic_key(target):
        return "closer-evidence-semantic/v1:" + hashlib.sha256(target.encode()).hexdigest()

    return {
        "prepare": lambda target, ctx: {
            "revision": "rev-1",
            "identity": target,
            "semantic_cache_key": semantic_key(target),
        },
        "interpret": interpret or (lambda target, ctx: {"substantive_needs": []}),
        "act": act or (lambda target, ctx: {"status": "completed", "target_complete": True}),
        "exception": exception or (lambda target, ctx: {"reviewed": True}),
    }


def interrupted_round_fixture(path, at):
    """The 67-row interruption shape, with no invented historical stop proof."""
    rows = {}
    for index in range(33):
        rows[f"owner/repo#deadline-{index}"] = {
            "status": "deferred",
            "stage": "interpret",
            "results": {},
            "deferred_reason": "deadline_exhausted",
            "deferred_next_trigger": "next_round",
            "resource_evidence": {},
        }
    for index in range(27):
        rows[f"owner/repo#capacity-{index}"] = {
            "status": "deferred",
            "stage": "exception",
            "results": {},
            "deferred_reason": "worker_capacity",
            "deferred_next_trigger": "capacity_recheck",
            "resource_evidence": {
                "capacity": {
                    "lane": "exception",
                    "active": 0,
                    "limit": 0,
                    "available": 0,
                    "required": 1,
                    "sampled_at": at - 3600,
                }
            },
        }
    for index in range(3):
        rows[f"owner/repo#failed-act-{index}"] = {
            "status": "failed",
            "stage": "act",
            "results": {
                "act": {
                    "status": "blocked",
                    "reason": "worker_json_invalid",
                }
            },
            "error": "act returned unsupported or missing status",
            "deferred_next_trigger": "human_review",
        }
    for index in range(2):
        rows[f"owner/repo#uncertain-act-{index}"] = {
            "status": "uncertain",
            "stage": "act",
            "results": {},
            "deferred_next_trigger": "reconcile_before_retry",
        }
        rows[f"owner/repo#read-failed-{index}"] = {
            "status": "failed",
            "stage": "interpret",
            "results": {},
            "error": "RuntimeError: target absent from fresh closer assignment",
            "deferred_next_trigger": "human_review",
        }
    old_assignment = sorted(rows)
    for row in rows.values():
        row["metrics"] = {"waits": 0, "errors": 0}
    removed = {
        "owner/repo#failed-act-2",
        "owner/repo#uncertain-act-1",
        "owner/repo#read-failed-0",
        "owner/repo#read-failed-1",
    }
    current_assignment = sorted(set(old_assignment) - removed)
    state = {
        "schema": closer_pipeline.SCHEMA,
        "runs": {
            "interrupted": {
                "created": at - 3000,
                "deadline": at - 1000,
                "assignment": old_assignment,
                "targets": rows,
            }
        },
        "carry": {},
        "cache": {},
    }
    closer_pipeline._save(path, state)
    return current_assignment, removed


def test_expired_invalid_round_restarts_only_current_nonaction_work_and_keeps_audit(tmp_path):
    path = tmp_path / "pipeline.json"
    clock = [1_800_000_000.0]
    assignment, removed = interrupted_round_fixture(path, clock[0])
    before = validate_stop(path, sorted(assignment + list(removed)), now=lambda: clock[0])
    assert not before["valid"]
    assert len(before["reasons"]) == 65
    calls = []
    cb = callbacks(
        interpret=lambda target, _context: calls.append(("interpret", target))
        or {"substantive_needs": []},
        act=lambda target, _context: calls.append(("act", target))
        or {"status": "completed", "target_complete": True},
    )
    cb["prepare"] = lambda target, _context: calls.append(("prepare", target)) or {
        "revision": "fresh-revision",
        "identity": target,
    }
    first = run(
        path,
        assignment,
        cb,
        seconds=10,
        now=lambda: clock[0],
        reconcile=lambda target, _context: calls.append(("reconcile", target))
        or {"outcome": "unknown"},
    )
    assert first.get("status") != "blocked"
    assert len([call for call in calls if call[0] == "prepare"]) == 60
    assert len([call for call in calls if call[0] == "act"]) == 60
    assert [target for stage, target in calls if stage == "reconcile"] == [
        "owner/repo#uncertain-act-0"
    ]
    assert not any(
        target in removed or "failed-act" in target or "uncertain-act" in target
        for stage, target in calls
        if stage != "reconcile"
    )
    state = closer_pipeline._load(path)
    latest = next(iter(state["runs"].values()))
    assert latest["prior_stop"]["valid"] is False
    assert latest["prior_invalid_stop_evidence"]["valid"] is False
    audit = latest["invalid_stop_history"][0]
    assert audit["source_run_id"] == "interrupted"
    assert len(audit["targets"]) == 67
    assert audit["prior_stop"]["reasons"] == before["reasons"]
    assert latest["targets"]["owner/repo#failed-act-0"]["status"] == "failed"
    assert latest["targets"]["owner/repo#failed-act-0"]["recovery_barrier"]
    assert latest["targets"]["owner/repo#failed-act-2"]["recovery_excluded"] is True
    assert latest["targets"]["owner/repo#uncertain-act-1"]["recovery_excluded"] is True

    # A fresh assignment may reobserve completed targets; the action adapter
    # still owns source dedup. Unsafe prior actions and removed items stay barred.
    clock[0] += 20
    calls.clear()
    second = run(
        path,
        assignment,
        cb,
        seconds=10,
        now=lambda: clock[0],
        reconcile=lambda target, _context: calls.append(("reconcile", target))
        or {"outcome": "unknown"},
    )
    assert second.get("status") != "blocked"
    assert [target for stage, target in calls if stage == "reconcile"] == [
        "owner/repo#uncertain-act-0"
    ]
    assert not any(
        target in removed or "failed-act" in target or "uncertain-act" in target
        for stage, target in calls
        if stage != "reconcile"
    )
    latest = next(iter(closer_pipeline._load(path)["runs"].values()))
    assert latest["invalid_stop_history"][0] == audit
    assert latest["targets"]["owner/repo#failed-act-0"]["status"] == "failed"


@pytest.mark.parametrize(
    "mutation",
    [
        "unexpired",
        "assignment",
        "unknown_reason",
        "unsafe_operation",
        "created_bool",
        "created_nan",
        "created_infinite",
    ],
)
def test_expired_invalid_round_rejects_nonmatching_prior_state(tmp_path, mutation):
    path = tmp_path / "pipeline.json"
    at = 1_800_000_000.0
    assignment, _removed = interrupted_round_fixture(path, at)
    state = closer_pipeline._load(path)
    old = state["runs"]["interrupted"]
    if mutation == "unexpired":
        old["deadline"] = at + 100
    elif mutation == "assignment":
        old["assignment"].append("owner/repo#missing")
    elif mutation == "unknown_reason":
        old["targets"]["owner/repo#capacity-0"]["deferred_reason"] = "claim_held"
    elif mutation == "created_bool":
        old["created"] = True
    elif mutation == "created_nan":
        old["created"] = float("nan")
    elif mutation == "created_infinite":
        old["created"] = float("inf")
    else:
        old["targets"]["owner/repo#deadline-0"]["results"]["act"] = {
            "status": "uncertain",
            "operation_id": "unknown",
        }
    if mutation in {"unexpired", "created_bool", "created_nan", "created_infinite"}:
        prior = validate_stop(path, old["assignment"], now=lambda: at)
        assert not closer_pipeline._expired_invalid_round_recovery(
            prior, old, path, "interrupted", at
        )[0]
        return
    closer_pipeline._save(path, state)
    calls = []
    cb = callbacks(
        act=lambda target, _context: calls.append(target)
        or {"status": "completed", "target_complete": True}
    )
    result = run(path, assignment, cb, seconds=2, now=lambda: at)
    assert result.get("status") == "blocked"
    assert result["reason"] == "prior_run_has_no_valid_stop"
    assert not calls


def test_expired_invalid_round_barrier_must_match_immutable_item_lineage(tmp_path):
    path = tmp_path / "pipeline.json"
    at = 1_800_000_000.0
    assignment, _removed = interrupted_round_fixture(path, at)
    result = run(
        path,
        assignment,
        callbacks(),
        seconds=10,
        now=lambda: at,
        reconcile=lambda _target, _context: {"outcome": "unknown"},
    )
    assert result.get("status") != "blocked"
    state = closer_pipeline._load(path)
    run_id, previous = next(iter(state["runs"].items()))
    prior = validate_stop(path, assignment, now=lambda: at + 20)
    assert closer_pipeline._expired_invalid_round_recovery(
        prior,
        previous,
        path,
        run_id,
        at + 20,
    )[0]
    previous["targets"]["owner/repo#failed-act-0"]["revision"] = "substituted"
    assert not closer_pipeline._expired_invalid_round_recovery(
        prior,
        previous,
        path,
        run_id,
        at + 20,
    )[0]


def test_expired_invalid_round_rechecks_admission_before_old_semantic_cache(tmp_path):
    path = tmp_path / "pipeline.json"
    at = 1_800_000_000.0
    interrupted_round_fixture(path, at)
    target = "owner/repo#deadline-0"
    semantic = "closer-evidence-semantic/v1:" + hashlib.sha256(target.encode()).hexdigest()
    cache_key = closer_pipeline._digest(
        {
            "schema": "closer-pipeline-semantic-cache/v1",
            "target": target,
            "identity": target,
            "stage": "interpret",
            "semantic": semantic,
        }
    )
    state = closer_pipeline._load(path)
    state["cache"][cache_key] = {
        "value": {"substantive_needs": ["old approval"]},
        "target": target,
        "stage": "interpret",
    }
    closer_pipeline._save(path, state)
    called = []
    cb = callbacks(
        interpret=lambda name, _ctx: called.append(("interpret", name))
        or {
            "status": "pending",
            "pending_kind": "external_event",
            "next_trigger": "fresh campaign admission",
            "pending_evidence": {"event_id": "actual-admission-event"},
        },
        exception=lambda name, _ctx: called.append(("exception", name)) or {},
        act=lambda name, _ctx: called.append(("act", name)) or {},
    )
    cb["prepare"] = lambda name, _ctx: {
        "revision": "fresh-revision",
        "identity": name,
        "semantic_cache_key": semantic,
    }
    result = run(path, [target], cb, seconds=10, now=lambda: at)
    assert result.get("status") != "blocked"
    assert called == [("interpret", target)]
    assert (
        next(iter(closer_pipeline._load(path)["runs"].values()))["targets"][target]["status"]
        == "pending"
    )


def test_expired_invalid_round_history_does_not_grow_on_unchanged_cadences(tmp_path):
    path = tmp_path / "pipeline.json"
    at = 1_800_000_000.0
    interrupted_round_fixture(path, at)
    target = "owner/repo#deadline-0"
    origin = None
    sizes = []
    for cadence in range(10):
        current = at + 20 * cadence
        result = run(path, [target], callbacks(), seconds=10, now=lambda: current)
        assert result.get("status") != "blocked"
        latest = next(iter(closer_pipeline._load(path)["runs"].values()))
        history = latest["invalid_stop_history"]
        if origin is None:
            origin = history[0]
            assert origin["prior_stop"]["valid"] is False
            assert len(origin["targets"]) == 67
        assert history[0] == origin
        sizes.append(len(json.dumps(history)))
    assert len(set(sizes[2:])) == 1


def test_expired_invalid_round_archives_near_cap_history_without_losing_barrier(tmp_path):
    path = tmp_path / "pipeline.json"
    at = 1_800_000_000.0
    interrupted_round_fixture(path, at)
    target = "owner/repo#deadline-0"
    cb = callbacks()
    first = run(path, [target], cb, seconds=10, now=lambda: at)
    assert first.get("status") != "blocked"
    state = closer_pipeline._load(path)
    previous = next(iter(state["runs"].values()))
    origin = closer_pipeline._bounded(previous["invalid_stop_history"][0])
    # A retained, large prior material transition puts the next real change
    # over the in-ledger audit limit; it must move intact to the sidecar.
    padding = 512 * 1024 - len(closer_pipeline._canonical([origin])) - 3000
    older = {
        "schema": "closer-expired-invalid-round-recovery/v1",
        "source_run_id": "older-material-transition",
        "targets": {},
        "retained_evidence": "x" * padding,
    }
    previous["invalid_stop_history"].append(older)
    previous["targets"]["owner/repo#uncertain-act-0"]["revision"] = "new-material-revision"
    closer_pipeline._save(path, state)
    second = run(
        path,
        [target],
        cb,
        seconds=10,
        now=lambda: at + 20,
        reconcile=lambda _target, _ctx: {"outcome": "unknown"},
    )
    assert second.get("status") != "blocked"
    state = closer_pipeline._load(path)
    latest = next(iter(state["runs"].values()))
    assert latest["invalid_stop_history"][0] == origin
    assert latest["targets"]["owner/repo#failed-act-0"]["recovery_barrier"]
    archive = latest["invalid_stop_archive"]
    assert archive["count"] >= 2
    archived = closer_pipeline._archived_history(path, archive)
    assert older in archived
    assert any(entry.get("source_run_id") == first["run_id"] for entry in archived)
    third = run(
        path,
        [target],
        cb,
        seconds=10,
        now=lambda: at + 40,
        reconcile=lambda _target, _ctx: {"outcome": "unknown"},
    )
    assert third.get("status") != "blocked"
    latest = next(iter(closer_pipeline._load(path)["runs"].values()))
    assert latest["invalid_stop_history"][0] == origin
    chunk = closer_pipeline._audit_archive_dir(path) / f"{archive['sha256']}.json"
    chunk.write_bytes(b"{}")
    blocked = run(
        path,
        [target],
        cb,
        seconds=10,
        now=lambda: at + 60,
        reconcile=lambda _target, _ctx: {"outcome": "unknown"},
    )
    assert blocked.get("status") == "blocked"
    assert blocked["reason"] in {"prior_run_has_no_valid_stop", "audit_archive_unavailable"}


def test_expired_invalid_round_accepts_mixed_historically_valid_capacity(tmp_path):
    path = tmp_path / "pipeline.json"
    at = 1_800_000_000.0
    interrupted_round_fixture(path, at)
    state = closer_pipeline._load(path)
    row = state["runs"]["interrupted"]["targets"]["owner/repo#capacity-0"]
    row["resource_evidence"]["capacity"]["sampled_at"] = at - 10
    deadline_row = state["runs"]["interrupted"]["targets"]["owner/repo#deadline-0"]
    deadline_row["resource_evidence"] = {
        "type": "active_deadline",
        "deadline": at - 1000,
        "observed_at": at - 999,
    }
    closer_pipeline._save(path, state)
    old = state["runs"]["interrupted"]
    prior = validate_stop(path, old["assignment"], now=lambda: at)
    assert "unverified_resource_deferral:owner/repo#capacity-0" not in prior["reasons"]
    assert "unverified_deadline_boundary:owner/repo#deadline-0" not in prior["reasons"]
    assert not prior["valid"]
    assert closer_pipeline._expired_invalid_round_recovery(
        prior,
        old,
        path,
        "interrupted",
        at,
    )[0]
    called = []
    cb = callbacks(
        act=lambda name, _ctx: called.append(name)
        or {
            "status": "completed",
            "target_complete": True,
        }
    )
    result = run(
        path, ["owner/repo#capacity-0", "owner/repo#deadline-0"], cb, seconds=10, now=lambda: at
    )
    assert result.get("status") != "blocked"
    assert sorted(called) == ["owner/repo#capacity-0", "owner/repo#deadline-0"]


def test_expired_invalid_round_accepts_only_native_proven_provider_failure(tmp_path):
    path = tmp_path / "pipeline.json"
    at = 1_800_000_000.0
    interrupted_round_fixture(path, at)
    target = "owner/repo#deadline-0"
    state = closer_pipeline._load(path)
    old = state["runs"]["interrupted"]
    failure = {
        "profile_id": "low-a",
        "agent": "Gemini",
        "run_id": "native-run-123",
        "category": "transport",
        "evidence_hash": "e" * 64,
        "exit": 70,
    }
    value = {
        "status": "deferred",
        "reason": "provider_failure",
        "next_trigger": "retry_other_low_profile",
        "resource_evidence": {"provider_failure": failure},
    }
    old["targets"][target].update(
        deferred_reason="provider_failure",
        deferred_next_trigger=value["next_trigger"],
        resource_evidence=value["resource_evidence"],
        results={"interpret": value},
    )
    closer_pipeline._save(path, state)
    prior = validate_stop(path, old["assignment"], now=lambda: at)
    assert not prior["valid"]
    assert f"unverified_provider_failure:{target}" not in prior["reasons"]
    assert closer_pipeline._expired_invalid_round_recovery(
        prior,
        old,
        path,
        "interrupted",
        at,
    )[0]
    bad_old = closer_pipeline._bounded(old)
    bad_old["targets"][target]["resource_evidence"]["provider_failure"]["exit"] = 0
    bad_old["targets"][target]["results"]["interpret"]["resource_evidence"]["provider_failure"][
        "exit"
    ] = 0
    assert not closer_pipeline._expired_invalid_round_recovery(
        prior,
        bad_old,
        path,
        "interrupted",
        at,
    )[0]
    called = []
    cb = callbacks(
        act=lambda name, _ctx: called.append(name)
        or {
            "status": "completed",
            "target_complete": True,
        }
    )
    result = run(path, [target], cb, seconds=10, now=lambda: at)
    assert result.get("status") != "blocked"
    assert called == [target]


def test_expired_invalid_round_carries_real_validated_inner_wait(tmp_path):
    path = tmp_path / "pipeline.json"
    target = "owner/repo#failed-act-0"
    revision = "rev-wait"
    head = "a" * 40
    trigger = "source owner supplies current-head proof or resolves the named finding"
    inner_path = path.parent / f"{path.stem}.actions" / f"{closer_pipeline._digest(target)}.json"
    receipt = closer_queue.run_round(
        inner_path,
        [target],
        lambda _target: {
            "target": target,
            "revision": revision,
            "action": "wait",
            "eligible": False,
            "requires_coordinator": False,
            "reason": "worker_json_invalid",
            "next_trigger": trigger,
            "evidence": {"head": head, "labels": [], "sources": []},
        },
        lambda *_args: pytest.fail("wait cannot execute"),
        deadline=time.time() + 30,
        max_workers=1,
        max_passes=1,
    )
    proof = {
        "schema": closer_pipeline.INNER_WAIT_SCHEMA,
        "target": target,
        "revision": revision,
        "head": head,
        "reason": "worker_json_invalid",
        "next_trigger": trigger,
        "inner_path": str(inner_path),
        "receipt_sha256": closer_pipeline._digest(receipt),
    }
    at = time.time() + 10
    interrupted_round_fixture(path, at)
    state = closer_pipeline._load(path)
    old = state["runs"]["interrupted"]
    old["targets"][target] = {
        "status": "deferred",
        "stage": "act",
        "revision": revision,
        "identity": target,
        "deferred_reason": "validated_inner_wait",
        "deferred_next_trigger": trigger,
        "resource_evidence": {"inner_wait": proof},
        "results": {
            "prepare": {
                "revision": revision,
                "snapshot": {
                    "target": target,
                    "revision": revision,
                    "head": head,
                    "kind": "pr",
                },
            },
            "act": {
                "status": "deferred",
                "reason": "validated_inner_wait",
                "next_trigger": trigger,
                "resource_evidence": {"inner_wait": proof},
            },
        },
        "metrics": {"waits": 0, "errors": 0},
    }
    closer_pipeline._save(path, state)
    prior = validate_stop(path, old["assignment"], now=lambda: at)
    assert not prior["valid"]
    assert f"unverified_inner_wait:{target}" not in prior["reasons"]
    assert closer_pipeline._expired_invalid_round_recovery(
        prior,
        old,
        path,
        "interrupted",
        at,
    )[0]
    called = []
    failure = {
        "profile_id": "low-a",
        "agent": "Gemini",
        "run_id": "native-run-456",
        "category": "transport",
        "evidence_hash": "f" * 64,
        "exit": 70,
    }
    provider_wait = {
        "status": "deferred",
        "reason": "provider_failure",
        "next_trigger": "retry_other_low_profile",
        "resource_evidence": {"provider_failure": failure},
    }
    cb = callbacks(
        interpret=lambda _name, _ctx: provider_wait,
        act=lambda name, _ctx: called.append(name)
        or {
            "status": "completed",
            "target_complete": True,
        },
    )
    result = run(path, [target], cb, seconds=10, now=lambda: at)
    assert result.get("status") != "blocked"
    assert result["targets"][target]["deferred_reason"] == "provider_failure"
    assert called == []
    cb["interpret"] = lambda _name, _ctx: {"substantive_needs": []}
    result = run(path, [target], cb, seconds=10, now=lambda: at + 20)
    assert result.get("status") != "blocked"
    assert called == [target]


@pytest.mark.parametrize(
    ("reason", "labels"),
    [
        ("worker_json_invalid", []),
        ("current item-scoped hold: ['agents:paused']", ["agents:paused"]),
    ],
)
def test_validated_inner_wait_is_stop_valid_and_refreshes_next_cadence(tmp_path, reason, labels):
    target = "owner/repo#1"
    path = tmp_path / "pipeline.json"
    trigger = (
        "source owner supplies current-head proof or resolves the named finding"
        if reason == "worker_json_invalid"
        else "source owner removes agents:paused"
    )
    head = ["a" * 40]
    calls = []
    interpretation_calls = []

    def prepare(_target, _context):
        revision = "revision-" + head[0]
        return {
            "revision": revision,
            "identity": target,
            "semantic_cache_key": "closer-evidence-semantic/v1:"
            + hashlib.sha256(head[0].encode()).hexdigest(),
            "snapshot": {"target": target, "revision": revision, "head": head[0], "kind": "pr"},
        }

    def interpret(_target, _context):
        interpretation_calls.append(head[0])
        return {"classification": "deterministic_inner_wait", "substantive_needs": []}

    def act(_target, context):
        calls.append(head[0])
        inner_path = context["action_state_path"]
        receipt = closer_queue.run_round(
            inner_path,
            [target],
            lambda _item: {
                "target": target,
                "revision": context["revision"],
                "action": "wait",
                "eligible": False,
                "requires_coordinator": False,
                "reason": reason,
                "next_trigger": trigger,
                "evidence": {"head": head[0], "labels": labels, "sources": []},
            },
            lambda *_args: pytest.fail("inner wait must not execute an action"),
            deadline=time.time() + 30,
            max_workers=1,
            max_passes=1,
        )
        proof = {
            "schema": closer_pipeline.INNER_WAIT_SCHEMA,
            "target": target,
            "revision": context["revision"],
            "head": head[0],
            "reason": reason,
            "next_trigger": trigger,
            "inner_path": inner_path,
            "receipt_sha256": closer_pipeline._digest(receipt),
        }
        return {
            "status": "deferred",
            "reason": "validated_inner_wait",
            "next_trigger": trigger,
            "resource_evidence": {"inner_wait": proof},
        }

    cb = {
        "prepare": prepare,
        "interpret": interpret,
        "exception": lambda *_: pytest.fail("exception not needed"),
        "act": act,
    }
    first = run(path, [target], cb)
    assert validate_stop(path, [target])["stop_valid"] is True
    assert first["targets"][target]["deferred_reason"] == "validated_inner_wait"
    assert validate_stop(path, [target])["stop_valid"] is True
    # The old ordinary model result can be cached, but a validated wait still
    # needs today's fresh admission before reusing it.
    state = closer_pipeline._load(path)
    old_prep = first["targets"][target]["results"]["prepare"]
    cache_key = closer_pipeline._digest(
        {
            "schema": "closer-pipeline-semantic-cache/v1",
            "target": target,
            "identity": target,
            "stage": "interpret",
            "semantic": old_prep["semantic_cache_key"],
        }
    )
    state["cache"][cache_key] = {
        "target": target,
        "stage": "interpret",
        "value": {"classification": "relevant", "substantive_needs": []},
    }
    closer_pipeline._save(path, state)
    run(path, [target], cb)
    assert interpretation_calls == ["a" * 40, "a" * 40]
    assert validate_stop(path, [target])["stop_valid"] is True
    head[0] = "b" * 40
    run(path, [target], cb)
    assert validate_stop(path, [target])["stop_valid"] is True
    assert interpretation_calls == ["a" * 40, "a" * 40, "b" * 40]
    assert calls == ["a" * 40, "a" * 40, "b" * 40]


def test_validated_inner_wait_rejects_spoofed_or_stale_proof(tmp_path):
    target = "owner/repo#1"
    path = tmp_path / "pipeline.json"
    reason = "worker_json_invalid"
    trigger = "source owner supplies current-head proof or resolves the named finding"
    snapshot = {"target": target, "revision": "rev", "head": "a" * 40, "kind": "pr"}
    proof = {
        "schema": closer_pipeline.INNER_WAIT_SCHEMA,
        "target": target,
        "revision": "rev",
        "head": "a" * 40,
        "reason": reason,
        "next_trigger": trigger,
        "inner_path": str(
            path.parent / f"{path.stem}.actions" / f"{closer_pipeline._digest(target)}.json"
        ),
        "receipt_sha256": "0" * 64,
    }
    cb = {
        "prepare": lambda *_: {"revision": "rev", "identity": target, "snapshot": snapshot},
        "interpret": lambda *_: {"substantive_needs": []},
        "exception": lambda *_: pytest.fail("exception not needed"),
        "act": lambda *_: {
            "status": "deferred",
            "reason": "validated_inner_wait",
            "next_trigger": trigger,
            "resource_evidence": {"inner_wait": proof},
        },
    }
    receipt = run(path, [target], cb)
    assert validate_stop(path, [target])["stop_valid"] is False
    assert receipt["targets"][target]["error"] == "unverified_inner_wait"


def test_inner_wait_proof_rejects_changed_head_assignment_row_and_operation(tmp_path):
    target = "owner/repo#1"
    path = tmp_path / "pipeline.json"
    inner_path = path.parent / f"{path.stem}.actions" / f"{closer_pipeline._digest(target)}.json"
    reason = "worker_json_invalid"
    trigger = "source owner supplies current-head proof or resolves the named finding"
    head = "a" * 40
    snapshot = {"target": target, "revision": "rev", "head": head, "kind": "pr"}
    receipt = closer_queue.run_round(
        inner_path,
        [target],
        lambda _target: {
            "target": target,
            "revision": "rev",
            "action": "wait",
            "eligible": False,
            "requires_coordinator": False,
            "reason": reason,
            "next_trigger": trigger,
            "evidence": {"head": head},
        },
        lambda *_: pytest.fail("wait must not execute"),
        deadline=time.time() + 30,
        max_workers=1,
        max_passes=1,
    )
    proof = {
        "schema": closer_pipeline.INNER_WAIT_SCHEMA,
        "target": target,
        "revision": "rev",
        "head": head,
        "reason": reason,
        "next_trigger": trigger,
        "inner_path": str(inner_path),
        "receipt_sha256": closer_pipeline._digest(receipt),
    }

    def valid():
        return closer_pipeline._valid_inner_wait(
            path, target, "rev", snapshot, reason, trigger, proof
        )

    assert valid()
    assert not closer_pipeline._valid_inner_wait(
        path, target, "rev", {**snapshot, "head": "b" * 40}, reason, trigger, proof
    )
    assert not closer_pipeline._valid_inner_wait(
        path,
        target,
        "rev",
        snapshot,
        reason,
        trigger,
        {**proof, "inner_path": str(tmp_path / "other")},
    )
    assert not closer_pipeline._valid_inner_wait(
        path,
        target,
        "rev",
        snapshot,
        "generic blocked",
        trigger,
        {**proof, "reason": "generic blocked"},
    )
    original = closer_queue._read_state(inner_path)
    for mutate in (
        lambda state: state["round"].update(assignment_targets=["owner/repo#2"]),
        lambda state: state["round"].update(status="running"),
        lambda state: state["items"][target]["evidence"].update(head="b" * 40),
        lambda state: state["items"][target].update(
            operation={"id": "unknown", "status": "uncertain"}
        ),
        lambda state: state["items"][target].update(operation_history=["unknown"]),
        lambda state: state["round"]["receipt"]["accounting"][0].update(eligible=True),
    ):
        changed = json.loads(json.dumps(original))
        mutate(changed)
        closer_queue._atomic_write(inner_path, changed)
        assert not valid()
    closer_queue._atomic_write(inner_path, original)
    assert valid()


@pytest.mark.parametrize("tamper", [None, "generic_blocked", "missing_receipt", "uncertain_op"])
def test_legacy_failed_act_recovers_only_from_exact_durable_wait(tmp_path, tamper):
    target = "owner/repo#1"
    path = tmp_path / "pipeline.json"
    head = "a" * 40
    reason = "worker_json_invalid"
    trigger = "source owner supplies current-head proof or resolves the named finding"
    snapshot = {"target": target, "revision": "rev", "head": head, "kind": "pr"}
    first_receipt = []
    interpretation_calls = []

    def inner_wait(context):
        receipt = closer_queue.run_round(
            context["action_state_path"],
            [target],
            lambda _target: {
                "target": target,
                "revision": "rev",
                "action": "wait",
                "eligible": False,
                "requires_coordinator": False,
                "reason": reason,
                "next_trigger": trigger,
                "evidence": {"head": head},
            },
            lambda *_: pytest.fail("terminal wait must not execute"),
            deadline=time.time() + 30,
            max_workers=1,
            max_passes=1,
        )
        assert receipt["stop_valid"] is True
        return receipt

    def legacy_act(_target, context):
        receipt = inner_wait(context)
        first_receipt.append(receipt)
        return {
            "status": "blocked",
            "reason": reason,
            "next_trigger": trigger,
            "queue_receipt": receipt,
        }

    def interpret(*_):
        interpretation_calls.append(True)
        return {"classification": "relevant", "substantive_needs": []}

    cb = {
        "prepare": lambda *_: {
            "revision": "rev",
            "identity": target,
            "snapshot": snapshot,
            "semantic_cache_key": "closer-evidence-semantic/v1:" + "1" * 64,
        },
        "interpret": interpret,
        "exception": lambda *_: pytest.fail("exception not needed"),
        "act": legacy_act,
    }
    first = run(path, [target], cb)
    assert len(interpretation_calls) == 1
    assert first["targets"][target]["status"] == "failed"
    assert first["targets"][target]["stage"] == "act"
    assert not validate_stop(path, [target])["stop_valid"]

    inner_path = path.parent / f"{path.stem}.actions" / f"{closer_pipeline._digest(target)}.json"
    if tamper == "uncertain_op":
        inner = closer_queue._read_state(inner_path)
        inner["items"][target]["operation"] = {"id": "unresolved", "status": "uncertain"}
        closer_queue._atomic_write(inner_path, inner)
    elif tamper in {"generic_blocked", "missing_receipt"}:
        outer = closer_pipeline._load(path)
        old = next(iter(outer["runs"].values()))["targets"][target]["results"]["act"]
        if tamper == "generic_blocked":
            old["reason"] = "generic blocked"
        else:
            old.pop("queue_receipt")
        closer_pipeline._save(path, outer)

    def current_act(_target, context):
        receipt = inner_wait(context)
        proof = {
            "schema": closer_pipeline.INNER_WAIT_SCHEMA,
            "target": target,
            "revision": "rev",
            "head": head,
            "reason": reason,
            "next_trigger": trigger,
            "inner_path": context["action_state_path"],
            "receipt_sha256": closer_pipeline._digest(receipt),
        }
        return {
            "status": "deferred",
            "reason": "validated_inner_wait",
            "next_trigger": trigger,
            "resource_evidence": {"inner_wait": proof},
        }

    second = run(path, [target], {**cb, "act": current_act})
    if tamper:
        assert second["status"] == "blocked"
        assert second["reason"] == "prior_run_has_no_valid_stop"
        assert len(interpretation_calls) == 1
    else:
        assert len(interpretation_calls) == 2  # old model cache cannot skip new admission
        assert second["targets"][target]["deferred_reason"] == "validated_inner_wait"
        assert validate_stop(path, [target])["stop_valid"]
        state = closer_pipeline._load(path)
        newest = next(iter(state["runs"].values()))
        audit = newest["recovered_invalid_stop"]
        assert audit["targets"][target]["receipt_sha256"] == closer_pipeline._digest(
            first_receipt[0]
        )
        assert newest["prior_invalid_stop_evidence"]["valid"] is False


def run(path, targets, cb=None, *, capacity=None, seconds=2, **kw):
    cb = cb or callbacks()
    clock = kw.pop("now", time.time)
    return run_pipeline(
        path,
        targets,
        **cb,
        capacity_fn=lambda: capacity or {"io": 2, "low": 2, "exception": 1},
        deadline=clock() + seconds,
        now=clock,
        **kw,
    )


def run_bounded(call, *, timeout=60):
    """Run a scheduler stress fixture without turning its wall budget into a benchmark."""
    finished = threading.Event()
    result = []

    def invoke():
        try:
            result.append((True, call()))
        except BaseException as exc:  # re-raise worker failures in the test thread
            result.append((False, exc))
        finally:
            finished.set()

    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    assert finished.wait(timeout), f"scheduler stress fixture exceeded {timeout}s safety bound"
    succeeded, value = result[0]
    if not succeeded:
        raise value
    return value


def test_cheap_reports_skip_exception_and_complete(tmp_path):
    calls = []
    cb = callbacks(
        act=lambda t, c: calls.append(t) or {"status": "completed", "target_complete": True}
    )
    receipt = run(tmp_path / "state.json", ["a", "b"], cb)
    assert set(calls) == {"a", "b"}
    assert all(x["status"] == "completed" for x in receipt["targets"].values())
    assert all("exception" not in x["results"] for x in receipt["targets"].values())


def test_explicit_unknown_need_routes_exception_but_cannot_self_certify(tmp_path):
    seen = []
    cb = callbacks(
        interpret=lambda t, c: {"substantive_needs": ["unknown semantics"]},
        exception=lambda t, c: {"accept": True},
        act=lambda t, c: seen.append(c) or {"status": "completed", "target_complete": True},
    )
    receipt = run(tmp_path / "state.json", ["a"], cb)
    row = receipt["targets"]["a"]
    assert "exception" in row["results"] and seen
    assert row["status"] == "completed"


def test_missing_interpretation_schema_escalates_as_unknown(tmp_path):
    exceptions = []
    cb = callbacks(
        interpret=lambda t, c: {"summary": "looks fine"},
        exception=lambda t, c: exceptions.append(t) or {"reviewed": True},
    )
    run(tmp_path / "state.json", ["a"], cb)
    assert exceptions == ["a"]


def test_one_act_transition_reprepares_and_continues_in_same_budget(tmp_path):
    prepared, acted = [], []
    cb = callbacks(
        act=lambda t, c: acted.append(t)
        or (
            {"status": "completed", "progress_key": f"step-{len(acted)}"}
            if len(acted) < 3
            else {"status": "completed", "target_complete": True}
        )
    )
    cb["prepare"] = lambda t, c: prepared.append(t) or {
        "revision": f"r{len(prepared)}",
        "identity": t,
    }
    receipt = run(tmp_path / "state.json", ["a"], cb, seconds=2)
    assert acted == ["a", "a", "a"]
    assert len(prepared) == 3
    assert receipt["targets"]["a"]["status"] == "completed"


def test_changed_assignment_carries_unknown_action_for_reconciliation(tmp_path):
    path = tmp_path / "state.json"
    cb = callbacks(act=lambda t, c: (_ for _ in ()).throw(RuntimeError("unknown")))
    first = run(path, ["a"], cb)
    assert first["targets"]["a"]["status"] == "uncertain"
    acts, reconciled = [], []
    cb2 = callbacks(
        act=lambda t, c: acts.append(t) or {"status": "completed", "target_complete": True}
    )
    receipt = run_pipeline(
        path,
        ["b"],
        **cb2,
        reconcile=lambda t, c: reconciled.append(t)
        or {"outcome": "confirmed_completed", "target_complete": True},
        capacity_fn=lambda: {"io": 2, "low": 2, "exception": 1},
        deadline=time.time() + 2,
    )
    assert receipt["assignment"] == ["b"]
    assert "a" in receipt["carry_targets"]
    assert reconciled == ["a"] and acts == ["b"]


def test_expired_uncertain_run_allows_new_round_read_only_reconciliation(tmp_path):
    path = tmp_path / "state.json"
    acts = []
    first = run(
        path,
        ["a"],
        callbacks(
            act=lambda target, _ctx: acts.append(target)
            or (_ for _ in ()).throw(RuntimeError("action response was lost"))
        ),
    )
    assert first["targets"]["a"]["status"] == "uncertain"
    doc = json.loads(path.read_text())
    doc["runs"][first["run_id"]]["deadline"] = time.time() - 1
    path.write_text(json.dumps(doc))

    reconciled = []
    resumed = run_pipeline(
        path,
        ["a"],
        **callbacks(
            act=lambda target, _ctx: acts.append(target)
            or {"status": "completed", "target_complete": True}
        ),
        reconcile=lambda target, _ctx: reconciled.append(target)
        or {"outcome": "confirmed_not_started"},
        capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
        deadline=time.time() + 2,
    )
    assert reconciled == ["a"]
    assert acts == ["a", "a"]
    assert resumed["targets"]["a"]["status"] == "completed"


def test_completed_generation_reuses_revision_bound_interpretation(tmp_path):
    path = tmp_path / "state.json"
    interpretations = []
    cb = callbacks(
        interpret=lambda t, c: interpretations.append(c["revision"]) or {"substantive_needs": []}
    )
    run(path, ["a"], cb)
    run(path, ["a"], cb)
    assert interpretations == ["rev-1"]


def test_semantic_cache_covers_large_queue_and_only_refreshes_changed_evidence(tmp_path):
    path = tmp_path / "state.json"
    targets = [f"t{i:02}" for i in range(40)]
    changed = {"target": None}
    interpretations = []
    cb = callbacks(
        interpret=lambda t, c: interpretations.append(t) or {"substantive_needs": []},
    )
    cb["prepare"] = lambda t, c: {
        "revision": "same-revision",
        "identity": t,
        "proof": "changed" if t == changed["target"] else "stable",
        "semantic_cache_key": "closer-evidence-semantic/v1:"
        + hashlib.sha256(
            f"{t}:{'changed' if t == changed['target'] else 'stable'}".encode()
        ).hexdigest(),
    }

    def cap():
        return {"io": 8, "low": 4, "exception": 2}

    logical_now = [1_700_000_000.0]
    run_bounded(
        lambda: run_pipeline(
            path,
            targets,
            **cb,
            capacity_fn=cap,
            deadline=logical_now[0] + 300,
            now=lambda: logical_now[0],
        )
    )
    assert len(interpretations) == 40
    changed["target"] = "t17"
    logical_now[0] += 1
    run_bounded(
        lambda: run_pipeline(
            path,
            targets,
            **cb,
            capacity_fn=cap,
            deadline=logical_now[0] + 300,
            now=lambda: logical_now[0],
        )
    )
    assert len(interpretations) == 41
    assert interpretations.count("t17") == 2


def test_semantic_cache_ignores_packet_churn_but_binds_head_source_and_attempt(tmp_path):
    path = tmp_path / "state.json"
    semantic = {"head": "h1", "source": "src1", "attempt": "attempt1"}
    prepare_count = [0]
    interpretations, packet_refs = [], []

    def prepare(target, ctx):
        prepare_count[0] += 1
        binding = {"schema": "closer-evidence-semantic/v1", "target": target, **semantic}
        return {
            "revision": f"rev-{prepare_count[0]}",
            "identity": target,
            "semantic_cache_key": "closer-evidence-semantic/v1:"
            + hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest(),
            "packet_ref": f"packet-{prepare_count[0]}",
            "elapsed_ms": prepare_count[0],
            "captured_at": prepare_count[0],
            "history": [prepare_count[0]],
        }

    cb = callbacks(
        interpret=lambda t, c: interpretations.append(c["prepare"]["packet_ref"])
        or {"substantive_needs": []},
        act=lambda t, c: packet_refs.append(c["prepare"]["packet_ref"])
        or {"status": "completed", "target_complete": True},
    )
    cb["prepare"] = prepare

    def cycle():
        return run_pipeline(
            path,
            ["a"],
            **cb,
            capacity_fn=lambda: {"io": 2, "low": 2, "exception": 1},
            deadline=time.time() + 2,
        )

    cycle()
    cycle()  # refreshed packet_ref/elapsed/history, stable semantic binding
    assert len(interpretations) == 1
    for field, value in (("head", "h2"), ("source", "src2"), ("attempt", "attempt2")):
        semantic[field] = value
        cycle()
    assert len(interpretations) == 4
    assert len(set(packet_refs)) == 5  # actions always receive freshly prepared packet refs


def test_semantic_cache_requires_versioned_sha256_binding(tmp_path):
    path = tmp_path / "state.json"
    interpretations = []
    cb = callbacks(interpret=lambda t, c: interpretations.append(t) or {"substantive_needs": []})
    cb["prepare"] = lambda t, c: {
        "revision": "same",
        "identity": t,
        "semantic_cache_key": "closer-evidence-semantic/v1:not-a-digest",
    }
    run(path, ["a"], cb)
    run(path, ["a"], cb)
    assert interpretations == ["a", "a"]


def test_assignment_larger_than_legacy_cap_is_serviced_whole(tmp_path):
    targets = [f"t{i:03}" for i in range(80)]
    logical_now = 1_000_000.0
    receipt = run_bounded(
        lambda: run(
            tmp_path / "state.json",
            targets,
            seconds=300,
            now=lambda: logical_now,
        )
    )
    assert receipt["assignment"] == targets
    assert len(receipt["targets"]) == len(targets)
    assert all(row["status"] == "completed" for row in receipt["targets"].values())


def test_io_single_slot_advances_ready_actions_before_full_queue_preparation(tmp_path):
    targets = [f"t{i:03}" for i in range(80)]
    lock = threading.Lock()
    prepared = 0
    acted = 0
    early_actions = []
    active_io = 0
    max_io = 0

    def enter_io():
        nonlocal active_io, max_io
        with lock:
            active_io += 1
            max_io = max(max_io, active_io)

    def leave_io():
        nonlocal active_io
        with lock:
            active_io -= 1

    def prepare(target, context):
        nonlocal prepared
        enter_io()
        try:
            time.sleep(0.004)
            with lock:
                prepared += 1
            return {
                "revision": target,
                "identity": target,
                "semantic_cache_key": "closer-evidence-semantic/v1:"
                + hashlib.sha256(target.encode()).hexdigest(),
            }
        finally:
            leave_io()

    def act(target, context):
        nonlocal acted
        enter_io()
        try:
            with lock:
                if prepared < len(targets):
                    early_actions.append(target)
                acted += 1
            return {"status": "completed", "target_complete": True}
        finally:
            leave_io()

    cb = callbacks(act=act)
    cb["prepare"] = prepare
    logical_now = 2_000_000.0
    receipt = run_bounded(
        lambda: run_pipeline(
            tmp_path / "state.json",
            targets,
            **cb,
            capacity_fn=lambda: {"io": 1, "low": 4, "exception": 1},
            deadline=logical_now + 300,
            max_io_workers=1,
            max_low_workers=4,
            now=lambda: logical_now,
        )
    )
    assert prepared == len(targets) and acted == len(targets)
    assert early_actions, "ready actions waited for full-assignment preparation"
    assert max_io == 1
    assert all(row["status"] == "completed" for row in receipt["targets"].values())


def test_io_four_slots_can_run_ready_action_while_preparations_are_active(tmp_path):
    targets = [f"t{i:03}" for i in range(20)]
    slow_prepares = threading.Event()
    action_started = threading.Event()
    cb = callbacks()

    def prepare(target, context):
        if target != "t000":
            slow_prepares.set()
            assert action_started.wait(15), "ready action never used the freed IO slot"
        return {
            "revision": target,
            "identity": target,
            "semantic_cache_key": "closer-evidence-semantic/v1:"
            + hashlib.sha256(target.encode()).hexdigest(),
        }

    cb["prepare"] = prepare
    cb["act"] = lambda target, context: action_started.set() or {
        "status": "completed",
        "target_complete": True,
    }
    logical_now = time.time()
    receipt = run_bounded(
        lambda: run_pipeline(
            tmp_path / "state.json",
            targets,
            **cb,
            capacity_fn=lambda: {"io": 4, "low": 2, "exception": 1},
            deadline=logical_now + 300,
            max_io_workers=4,
            now=lambda: logical_now,
        ),
        timeout=30,
    )
    assert slow_prepares.is_set(), "preparations never overlapped the ready action"
    assert action_started.is_set(), "ready action failed to use the fourth IO slot"
    assert all(row["status"] == "completed" for row in receipt["targets"].values())


def test_completion_drives_next_stage_without_waiting_for_slow_peer(tmp_path):
    gate = threading.Event()
    slow_entered = threading.Event()
    fast_act = threading.Event()

    def prep(target, ctx):
        if target == "slow":
            slow_entered.set()
            gate.wait(30)
        return {"revision": target}

    cb = callbacks(
        act=lambda t, c: (fast_act.set() if t == "fast" else None)
        or {"status": "completed", "target_complete": True}
    )
    cb["prepare"] = prep
    receipts = []
    logical_now = time.time()
    worker = threading.Thread(
        target=lambda: receipts.append(
            run_pipeline(
                tmp_path / "state.json",
                ["slow", "fast"],
                **cb,
                capacity_fn=lambda: {"io": 2, "low": 2, "exception": 1},
                deadline=logical_now + 300,
                max_io_workers=2,
                max_low_workers=2,
                now=lambda: logical_now,
            )
        ),
        daemon=True,
    )
    worker.start()
    try:
        assert slow_entered.wait(15), "slow target never entered its blocked prepare"
        assert fast_act.wait(15), "fast target stalled behind slow target"
        assert not gate.is_set(), "slow peer was released before the fast action"
    finally:
        gate.set()
        worker.join(15)
    assert not worker.is_alive(), "pipeline did not finish within the hang guard"
    receipt = receipts[0]
    assert receipt["targets"]["fast"]["status"] == "completed"


def test_capacity_is_read_fresh_and_zero_defers(tmp_path):
    calls = []
    cb = callbacks(
        act=lambda t, c: calls.append(t) or {"status": "completed", "target_complete": True}
    )
    receipt = run(
        tmp_path / "state.json",
        ["a"],
        cb,
        capacity={"io": 0, "low": 0, "exception": 0},
        seconds=0.06,
    )
    assert not calls
    assert receipt["targets"]["a"]["status"] == "deferred"


def test_zero_low_capacity_yields_without_waiting_for_long_deadline(tmp_path):
    path = tmp_path / "state.json"
    started = time.monotonic()
    receipt = run_pipeline(
        path,
        ["a"],
        **callbacks(),
        capacity_fn=lambda: {"io": 1, "low": 0, "exception": 1},
        deadline=time.time() + 30,
    )
    assert time.monotonic() - started < 2
    assert receipt["targets"]["a"]["status"] == "deferred"
    assert receipt["targets"]["a"]["deferred_reason"] == "worker_capacity"
    assert validate_stop(path, ["a"])["valid"]


def test_interpret_worker_capacity_deferral_is_not_cached_across_cadences(tmp_path):
    path = tmp_path / "state.json"
    semantic_key = "closer-evidence-semantic/v1:" + hashlib.sha256(b"a").hexdigest()
    interpreted = []
    acted = []

    def interpret(_target, _context):
        interpreted.append(True)
        if len(interpreted) == 1:
            return {
                "status": "deferred",
                "reason": "worker_capacity",
                "next_trigger": "capacity_recheck",
                "resource_evidence": {
                    "capacity": {
                        "lane": "low",
                        "available": 0,
                        "required": 1,
                        "sampled_at": time.time(),
                    }
                },
                "classification": "capacity_deferred",
                "substantive_needs": [],
            }
        return {"substantive_needs": []}

    cb = callbacks(
        interpret=interpret,
        act=lambda target, _context: acted.append(target)
        or {"status": "completed", "target_complete": True},
    )
    # Keep the evidence identity stable across invocations, exactly as a
    # capacity-only recovery should.
    cb["prepare"] = lambda target, _context: {
        "revision": "rev-1",
        "identity": target,
        "semantic_cache_key": semantic_key,
    }

    first = run(path, ["a"], cb)
    assert first["targets"]["a"]["status"] == "deferred"
    assert first["targets"]["a"]["deferred_reason"] == "worker_capacity"
    assert validate_stop(path, ["a"])["valid"]
    assert not acted

    second = run(path, ["a"], cb)
    assert second["targets"]["a"]["status"] == "completed"
    assert interpreted == [True, True]
    assert acted == ["a"]


def test_interpretation_external_event_pending_short_circuits_exception_and_act(tmp_path):
    path = tmp_path / "state.json"
    calls = []
    cb = callbacks(
        interpret=lambda t, c: {
            "status": "pending",
            "pending_kind": "external_event",
            "next_trigger": "source_owner_permit",
            "pending_evidence": {"event_id": f"campaign-permit:{t}:{c['revision']}"},
        },
        exception=lambda t, c: calls.append("exception") or {"reviewed": True},
        act=lambda t, c: calls.append("act") or {"status": "completed", "target_complete": True},
    )
    receipt = run(path, ["a"], cb)
    assert receipt["targets"]["a"]["status"] == "pending"
    assert not calls
    assert validate_stop(path, ["a"])["valid"]


def test_external_event_reruns_pending_admission_instead_of_reusing_cache(tmp_path):
    path = tmp_path / "state.json"
    permit = {"ready": False}
    interpretations = []
    cb = callbacks(
        interpret=lambda t, c: (
            interpretations.append(t)
            or (
                {"substantive_needs": []}
                if permit["ready"]
                else {
                    "status": "pending",
                    "pending_kind": "external_event",
                    "next_trigger": "source_owner_permit",
                    "pending_evidence": {"event_id": f"campaign-permit:{t}:{c['revision']}"},
                }
            )
        ),
    )
    first = run(path, ["a"], cb)
    assert first["targets"]["a"]["status"] == "pending"
    permit["ready"] = True
    second = run(path, ["a"], cb)
    assert second["targets"]["a"]["status"] == "completed"
    assert interpretations == ["a", "a"]


def test_provider_failure_defers_without_strong_or_action_and_requires_evidence(tmp_path):
    path = tmp_path / "state.json"
    calls = []
    failure = {
        "profile_id": "low-profile",
        "agent": "provider-a",
        "run_id": "dispatch-123",
        "exit": 1,
        "category": "transport_error",
        "evidence_hash": "evidence-sha256",
    }
    cb = callbacks(
        interpret=lambda t, c: {
            "status": "deferred",
            "reason": "provider_failure",
            "next_trigger": "retry_profile_later",
            "resource_evidence": {"provider_failure": failure},
        },
        exception=lambda t, c: calls.append("exception") or {"reviewed": True},
        act=lambda t, c: calls.append("act") or {"status": "completed", "target_complete": True},
    )
    receipt = run(path, ["a"], cb)
    assert receipt["targets"]["a"]["deferred_reason"] == "provider_failure"
    assert not calls
    assert validate_stop(path, ["a"])["valid"]

    path2 = tmp_path / "invalid.json"
    bad = dict(failure, evidence_hash="")
    cb["interpret"] = lambda t, c: {
        "status": "deferred",
        "reason": "provider_failure",
        "next_trigger": "retry_profile_later",
        "resource_evidence": {"provider_failure": bad},
    }
    invalid = run(path2, ["a"], cb)
    assert invalid["targets"]["a"]["status"] == "failed"
    assert not validate_stop(path2, ["a"])["valid"]


def test_uncertain_native_interpretation_is_durable_and_never_retried(tmp_path):
    path = tmp_path / "uncertain-provider.json"
    clock = [1000.0]
    semantic = "closer-evidence-semantic/v1:" + "a" * 64
    head = "b" * 40
    calls = []

    def prepare(target, _context):
        calls.append(("prepare", target))
        return {
            "revision": "head-revision-1",
            "identity": target,
            "semantic_cache_key": semantic,
            "snapshot": {"head": head, "base": "c" * 40},
        }

    def interpret(target, context):
        calls.append(("interpret", target))
        return {
            "status": "uncertain",
            "classification": "provider_outcome_uncertain",
            "substantive_needs": [],
            "authority": False,
            "provider_outcome": {
                "schema": "closer-provider-outcome-uncertain/v1",
                "target": target,
                "head": head,
                "base": "c" * 40,
                "revision": context["revision"],
                "identity": context["identity"],
                "semantic_cache_key": semantic,
                "actual": {
                    "agent": "gemini",
                    "requested_profile": "gemini-low",
                    "run_id": "agy-turn-still-running-77",
                    "exit": 124,
                    "outcome_status": "uncertain",
                    "retry_safe": False,
                },
            },
        }

    cb = callbacks(interpret=interpret)
    cb["prepare"] = prepare
    cb["exception"] = lambda *_: calls.append(("exception", "a")) or {"reviewed": True}
    cb["act"] = lambda *_: calls.append(("act", "a")) or {
        "status": "completed",
        "target_complete": True,
    }
    receipt = run(path, ["a"], cb, seconds=60, now=lambda: clock[0])

    row = receipt["targets"]["a"]
    assert row["status"] == "uncertain"
    assert row["stage"] == "interpret"
    assert row["provider_outcome_uncertain"]["actual"]["run_id"] == ("agy-turn-still-running-77")
    stop = validate_stop(path, ["a"], now=lambda: clock[0])
    assert not stop["valid"]
    assert stop["continuation_required"] is True
    assert stop["reasons"] == ["unresolved_native_provider_outcome:a:agy-turn-still-running-77"]
    assert calls == [("prepare", "a"), ("interpret", "a")]

    # A later cadence with a changed assignment/head cannot treat the local
    # transport receipt as native completion or launch another model/action.
    clock[0] += 1
    resumed = run(
        path,
        ["a"],
        callbacks(
            interpret=lambda *_: calls.append(("retry", "a")) or {"substantive_needs": []},
            exception=lambda *_: calls.append(("exception-retry", "a")) or {},
            act=lambda *_: calls.append(("act-retry", "a")) or {},
        ),
        seconds=60,
        now=lambda: clock[0],
    )
    assert resumed["targets"]["a"]["status"] == "uncertain"
    assert calls == [("prepare", "a"), ("interpret", "a")]

    clock[0] += 61
    malformed_path = tmp_path / "malformed-provider-outcome.json"
    malformed_state = json.loads(path.read_text())
    malformed_run = next(iter(malformed_state["runs"].values()))
    malformed_run["targets"]["a"]["provider_outcome_uncertain"]["head"] = "d" * 40
    malformed_path.write_text(json.dumps(malformed_state))
    malformed = run(
        malformed_path,
        ["a", "new-head"],
        callbacks(),
        seconds=60,
        now=lambda: clock[0],
    )
    assert malformed["status"] == "blocked"
    assert malformed["reason"] == "prior_run_has_no_valid_stop"

    unrelated_calls = []
    next_callbacks = callbacks(
        interpret=lambda target, _context: unrelated_calls.append(("interpret", target))
        or {"substantive_needs": []},
        act=lambda target, _context: unrelated_calls.append(("act", target))
        or {"status": "completed", "target_complete": True},
    )
    next_callbacks["prepare"] = lambda target, _context: unrelated_calls.append(
        ("prepare", target)
    ) or {
        "revision": "rev-2",
        "identity": target,
        "semantic_cache_key": "closer-evidence-semantic/v1:"
        + hashlib.sha256(target.encode()).hexdigest(),
        "snapshot": {"head": "e" * 40},
    }
    continued = run(
        path,
        ["a", "new-head"],
        next_callbacks,
        seconds=60,
        now=lambda: clock[0],
    )
    assert continued["targets"]["a"]["status"] == "uncertain"
    assert continued["targets"]["new-head"]["status"] == "completed"
    assert unrelated_calls == [
        ("prepare", "new-head"),
        ("interpret", "new-head"),
        ("act", "new-head"),
    ]
    latest_run = json.loads(path.read_text())["runs"]
    latest = next(iter(latest_run.values()))
    assert latest["prior_invalid_stop_evidence"] == {
        "valid": False,
        "reasons": ["unresolved_native_provider_outcome:a:agy-turn-still-running-77"],
        "continuation_required": True,
    }
    continued_stop = validate_stop(path, ["a", "new-head"], now=lambda: clock[0])
    assert not continued_stop["valid"]
    assert continued_stop["continuation_required"] is True
    assert continued_stop["reasons"] == [
        "unresolved_native_provider_outcome:a:agy-turn-still-running-77"
    ], continued["targets"]["a"]
    assert calls == [("prepare", "a"), ("interpret", "a")]


def test_issue_uncertainty_without_head_stays_bound_by_revision_after_restart(tmp_path):
    path = tmp_path / "uncertain-issue.json"
    clock = [1000.0]
    target = "owner/repo#17"
    semantic = "closer-evidence-semantic/v1:" + "f" * 64
    calls = []

    def prepare(name, _context):
        calls.append(("prepare", name))
        return {
            "revision": "issue-revision-1",
            "identity": name,
            "semantic_cache_key": semantic,
            "snapshot": {"kind": "issue", "head": None, "base": None},
        }

    def interpret(name, context):
        calls.append(("interpret", name))
        return {
            "status": "uncertain",
            "provider_outcome": {
                "schema": "closer-provider-outcome-uncertain/v1",
                "target": name,
                "head": None,
                "revision": context["revision"],
                "identity": context["identity"],
                "semantic_cache_key": semantic,
                "actual": {
                    "agent": "gemini",
                    "requested_profile": "gemini-low",
                    "run_id": "agy-issue-turn-17",
                    "exit": 124,
                    "outcome_status": "uncertain",
                    "retry_safe": False,
                },
            },
        }

    first_callbacks = callbacks(interpret=interpret)
    first_callbacks["prepare"] = prepare
    first = run(path, [target], first_callbacks, seconds=60, now=lambda: clock[0])
    assert first["targets"][target]["status"] == "uncertain"
    assert validate_stop(path, [target], now=lambda: clock[0])["reasons"] == [
        f"unresolved_native_provider_outcome:{target}:agy-issue-turn-17"
    ]

    # Expire the first round so the next cadence takes the full prior-stop and
    # carry path. The issue's no-head binding must survive in the carried prepare.
    clock[0] += 61
    resumed_calls = []
    resumed_callbacks = callbacks(
        interpret=lambda name, _ctx: resumed_calls.append(("interpret", name))
        or {"substantive_needs": []},
        act=lambda name, _ctx: resumed_calls.append(("act", name))
        or {"status": "completed", "target_complete": True},
    )
    resumed_callbacks["prepare"] = lambda name, _ctx: resumed_calls.append(("prepare", name)) or {
        "revision": "unrelated-revision",
        "identity": name,
        "semantic_cache_key": "closer-evidence-semantic/v1:"
        + hashlib.sha256(name.encode()).hexdigest(),
        "snapshot": {"kind": "pr", "head": "e" * 40},
    }
    resumed = run(
        path,
        [target, "owner/repo#18"],
        resumed_callbacks,
        seconds=60,
        now=lambda: clock[0],
    )

    assert resumed["targets"][target]["status"] == "uncertain"
    assert resumed_calls == [
        ("prepare", "owner/repo#18"),
        ("interpret", "owner/repo#18"),
        ("act", "owner/repo#18"),
    ]
    assert validate_stop(path, [target, "owner/repo#18"], now=lambda: clock[0])["reasons"] == [
        f"unresolved_native_provider_outcome:{target}:agy-issue-turn-17"
    ]


@pytest.mark.parametrize(
    ("kind", "malformation"),
    [
        ("pr", "missing_head"),
        ("unknown", "missing_head"),
        ("issue", "revision_mismatch"),
    ],
    ids=["pr-needs-head", "unknown-kind-needs-head", "issue-needs-revision-binding"],
)
def test_rejected_uncertainty_receipt_blocks_restart_and_never_retries(
    tmp_path, kind, malformation
):
    path = tmp_path / f"malformed-{kind}-{malformation}.json"
    clock = [2000.0]
    target = "owner/repo#19"
    semantic = "closer-evidence-semantic/v1:" + "a" * 64
    calls = []

    def prepare(name, _context):
        calls.append(("prepare", name))
        return {
            "revision": "issue-revision-19",
            "identity": name,
            "semantic_cache_key": semantic,
            "snapshot": {"kind": kind, "head": None},
        }

    def interpret(name, context):
        calls.append(("interpret", name))
        revision = context["revision"]
        if malformation == "revision_mismatch":
            revision = "different-revision"
        return {
            "status": "uncertain",
            "provider_outcome": {
                "schema": "closer-provider-outcome-uncertain/v1",
                "target": name,
                "head": None,
                "revision": revision,
                "identity": context["identity"],
                "semantic_cache_key": semantic,
                "actual": {
                    "agent": "gemini",
                    "requested_profile": "gemini-low",
                    "run_id": "agy-malformed-turn-19",
                    "exit": 124,
                    "outcome_status": "uncertain",
                    "retry_safe": False,
                },
            },
        }

    initial_callbacks = callbacks(interpret=interpret)
    initial_callbacks["prepare"] = prepare
    first = run(path, [target], initial_callbacks, seconds=60, now=lambda: clock[0])
    row = first["targets"][target]
    assert row["status"] == "failed"
    assert row["error"] == closer_pipeline.UNBOUND_UNCERTAIN_PROVIDER_ERROR
    stop = validate_stop(path, [target], now=lambda: clock[0])
    assert not stop["valid"]
    assert stop["reasons"] == [f"unresolved_pipeline_work:{target}:failed"]

    # A malformed unknown provider outcome is not reclassified as a read-only
    # failure. It blocks the next run before any existing or unrelated target can
    # launch another turn.
    clock[0] += 61
    restart_calls = []
    retry_callbacks = callbacks(
        interpret=lambda name, _ctx: restart_calls.append(("interpret", name))
        or {"substantive_needs": []},
    )
    retry_callbacks["prepare"] = lambda name, _ctx: restart_calls.append(("prepare", name)) or {
        "revision": "new-revision",
        "identity": name,
    }
    restart = run(
        path,
        [target, "owner/repo#20"],
        retry_callbacks,
        seconds=60,
        now=lambda: clock[0],
    )
    assert restart["status"] == "blocked"
    assert restart["reason"] == "prior_run_has_no_valid_stop"
    assert restart_calls == []
    assert calls == [("prepare", target), ("interpret", target)]


def test_mixed_native_uncertainty_and_readonly_failure_allow_only_safe_carry(tmp_path):
    path = tmp_path / "mixed-uncertainty.json"
    clock = [2000.0]
    native_head = "b" * 40
    native_semantic = "closer-evidence-semantic/v1:" + "a" * 64
    first_calls = []

    def first_prepare(target, _context):
        first_calls.append(("prepare", target))
        return {
            "revision": "rev-1",
            "identity": target,
            "semantic_cache_key": native_semantic,
            "snapshot": {"head": native_head, "base": "c" * 40},
        }

    def first_interpret(target, context):
        first_calls.append(("interpret", target))
        if target == "b":
            raise RuntimeError("read-only provider-result parsing failed")
        return {
            "status": "uncertain",
            "provider_outcome": {
                "schema": "closer-provider-outcome-uncertain/v1",
                "target": target,
                "head": native_head,
                "base": "c" * 40,
                "revision": context["revision"],
                "identity": context["identity"],
                "semantic_cache_key": native_semantic,
                "actual": {
                    "agent": "gemini",
                    "requested_profile": "gemini-low",
                    "run_id": "agy-mixed-uncertain-77",
                    "exit": 124,
                    "outcome_status": "uncertain",
                    "retry_safe": False,
                },
            },
        }

    first_callbacks = callbacks(interpret=first_interpret)
    first_callbacks["prepare"] = first_prepare
    first = run(path, ["a", "b"], first_callbacks, seconds=60, now=lambda: clock[0])
    assert first["targets"]["a"]["status"] == "uncertain"
    assert first["targets"]["b"]["status"] == "failed"
    first_stop = validate_stop(path, ["a", "b"], now=lambda: clock[0])
    assert not first_stop["valid"]
    assert set(first_stop["reasons"]) == {
        "unresolved_native_provider_outcome:a:agy-mixed-uncertain-77",
        "unresolved_pipeline_work:b:failed",
    }

    clock[0] += 61
    later_calls = []

    def later_prepare(target, _context):
        later_calls.append(("prepare", target))
        return {
            "revision": "rev-2",
            "identity": target,
            "semantic_cache_key": "closer-evidence-semantic/v1:"
            + hashlib.sha256(target.encode()).hexdigest(),
            "snapshot": {"head": "e" * 40, "base": "c" * 40},
        }

    later_callbacks = callbacks(
        interpret=lambda target, _context: later_calls.append(("interpret", target))
        or {"substantive_needs": []},
        act=lambda target, _context: later_calls.append(("act", target))
        or {"status": "completed", "target_complete": True},
    )
    later_callbacks["prepare"] = later_prepare
    resumed = run(
        path,
        ["a", "b", "c"],
        later_callbacks,
        seconds=60,
        now=lambda: clock[0],
    )
    assert resumed["targets"]["a"]["status"] == "uncertain"
    assert resumed["targets"]["b"]["status"] == "completed"
    assert resumed["targets"]["c"]["status"] == "completed"
    assert ("prepare", "a") not in later_calls
    assert ("interpret", "a") not in later_calls
    assert ("act", "a") not in later_calls
    assert {target for stage, target in later_calls if stage == "act"} == {"b", "c"}

    latest = next(iter(json.loads(path.read_text())["runs"].values()))
    assert latest["prior_invalid_stop_evidence"]["reasons"] == first_stop["reasons"]
    final_stop = validate_stop(path, ["a", "b", "c"], now=lambda: clock[0])
    assert not final_stop["valid"]
    assert final_stop["continuation_required"] is True
    assert final_stop["reasons"] == ["unresolved_native_provider_outcome:a:agy-mixed-uncertain-77"]


def test_exception_worker_capacity_defers_before_action(tmp_path):
    path = tmp_path / "state.json"
    calls = []
    sampled_at = time.time()
    cb = callbacks(
        interpret=lambda t, c: {"substantive_needs": ["needs strong review"]},
        exception=lambda t, c: {
            "status": "deferred",
            "reason": "worker_capacity",
            "next_trigger": "codex_pool_frees",
            "resource_evidence": {
                "capacity": {
                    "lane": "exception",
                    "active": 1,
                    "limit": 1,
                    "sampled_at": sampled_at,
                }
            },
        },
        act=lambda t, c: calls.append(t) or {"status": "completed", "target_complete": True},
    )
    receipt = run(path, ["a"], cb)
    assert receipt["targets"]["a"]["status"] == "deferred"
    assert receipt["targets"]["a"]["deferred_reason"] == "worker_capacity"
    assert calls == []
    assert validate_stop(path, ["a"])["valid"]
    assert not validate_stop(path, ["a"], now=lambda: sampled_at + 1801)["valid"]

    cb["exception"] = lambda t, c: {
        "status": "deferred",
        "reason": "worker_capacity",
        "next_trigger": "codex_pool_frees",
        "resource_evidence": {"capacity": {"lane": "exception"}},
    }
    invalid = run(tmp_path / "missing-proof.json", ["a"], cb)
    assert invalid["targets"]["a"]["status"] == "failed"
    assert not validate_stop(tmp_path / "missing-proof.json", ["a"])["valid"]


def test_capacity_decrease_limits_refill_after_each_completion(tmp_path):
    capacity_calls, starts = [], []
    started = threading.Event()
    zero_capacity_seen = threading.Event()
    release = threading.Event()
    logical_now = [10_000.0]
    deadline = logical_now[0] + 1

    def capacity():
        capacity_calls.append(1)
        amount = 1 if len(capacity_calls) == 1 else 0
        if amount == 0:
            zero_capacity_seen.set()
        return {"io": amount, "low": 2, "exception": 1}

    cb = callbacks()

    def prepare(target, context):
        starts.append(target)
        started.set()
        assert release.wait(15), "test failed to release the admitted callback"
        return {"revision": target}

    cb["prepare"] = prepare
    receipts = []
    worker = threading.Thread(
        target=lambda: receipts.append(
            run_pipeline(
                tmp_path / "state.json",
                ["a", "b"],
                **cb,
                capacity_fn=capacity,
                deadline=deadline,
                now=lambda: logical_now[0],
            )
        ),
        daemon=True,
    )
    worker.start()
    try:
        assert started.wait(15), "scheduler failed to submit the first read"
        assert zero_capacity_seen.wait(15), "scheduler never observed fresh zero capacity"
        logical_now[0] = deadline + 0.01
    finally:
        release.set()
        worker.join(15)
    assert not worker.is_alive(), "pipeline did not finish within the hang guard"
    receipt = receipts[0]
    assert len(capacity_calls) > 1
    assert len(starts) == 1
    assert sum(row["status"] == "completed" for row in receipt["targets"].values()) == 0


def test_same_state_race_never_duplicates_target_work(tmp_path):
    path = tmp_path / "state.json"
    seen = []
    active, max_active = 0, 0
    lock = threading.Lock()

    def slow(target, ctx):
        nonlocal active, max_active
        with lock:
            seen.append(target)
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.04)
        with lock:
            active -= 1
        return {"revision": "r"}

    cb = callbacks(act=lambda t, c: {"status": "completed"})
    cb["prepare"] = slow
    receipts = []

    def invoke():
        receipts.append(run(path, ["a"], cb))

    first = threading.Thread(target=invoke)
    second = threading.Thread(target=invoke)
    first.start()
    second.start()
    first.join()
    second.join()
    assert max_active == 1
    assert seen


def test_deadline_joins_inflight_callback_and_blocks_late_submissions(tmp_path):
    path = tmp_path / "state.json"
    entered, release, first_finished = threading.Event(), threading.Event(), threading.Event()
    second_invoked, second_entered = threading.Event(), threading.Event()
    first_starts, second_starts = [], []
    logical_now = [50_000.0]
    deadline = logical_now[0] + 1
    cb = callbacks()

    def prep(target, ctx):
        first_starts.append((target, logical_now[0]))
        if target == "a":
            entered.set()
            assert release.wait(15), "test failed to release the in-flight callback"
        return {"revision": target}

    cb["prepare"] = prep
    first_receipts = []
    first = threading.Thread(
        target=lambda: _run_and_mark_finished(),
        daemon=True,
    )

    def _run_and_mark_finished():
        try:
            first_receipts.append(
                run_pipeline(
                    path,
                    ["a", "b"],
                    **cb,
                    capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
                    deadline=deadline,
                    now=lambda: logical_now[0],
                )
            )
        finally:
            first_finished.set()

    first.start()
    second = None
    try:
        assert entered.wait(15), "first callback did not start within the hang guard"
        logical_now[0] = deadline + 1
        second_receipts = []

        second_cb = callbacks()

        def second_prepare(target, context):
            second_starts.append(target)
            second_entered.set()
            assert first_finished.is_set(), "second round entered before prior callbacks drained"
            return {"revision": target}

        second_cb["prepare"] = second_prepare

        def invoke_second():
            second_invoked.set()
            second_receipts.append(
                run_pipeline(
                    path,
                    ["a", "b"],
                    **second_cb,
                    capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
                    deadline=logical_now[0] + 10,
                    now=lambda: logical_now[0],
                )
            )

        second = threading.Thread(target=invoke_second, daemon=True)
        second.start()
        assert second_invoked.wait(15), "second round did not begin"
        assert not second_entered.wait(0.25), "a new round bypassed the old callback's run lock"
    finally:
        release.set()
        first.join(15)
        if second is not None:
            second.join(15)
    assert not first.is_alive() and second is not None and not second.is_alive()
    assert first_finished.is_set()
    assert first_receipts[0]["elapsed_seconds"] >= 1
    # The first round cannot submit the queued target's callback after cutoff.
    assert all(started_at < deadline for _, started_at in first_starts)
    assert [target for target, _ in first_starts] == ["a"]
    assert second_entered.is_set() and second_starts


def test_typed_prepare_exhaustion_waits_for_cutoff_and_defers_unstarted_targets(tmp_path):
    path = tmp_path / "pipeline.json"
    starts = []
    entered, release = threading.Event(), threading.Event()
    logical_now = [70_000.0]
    deadline = logical_now[0] + 1
    cb = callbacks()

    def prepare(target, context):
        starts.append(target)
        entered.set()
        assert release.wait(15), "test failed to release the admitted callback"
        remaining = deadline - logical_now[0]
        raise FakeRoundDeadlineExhausted(deadline, remaining)

    cb["prepare"] = prepare
    receipts = []
    worker = threading.Thread(
        target=lambda: receipts.append(
            run_pipeline(
                path,
                ["a", "b", "c"],
                **cb,
                capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
                deadline=deadline,
                max_io_workers=1,
                round_deadline_exception=FakeRoundDeadlineExhausted,
                now=lambda: logical_now[0],
            )
        ),
        daemon=True,
    )
    worker.start()
    try:
        assert entered.wait(15), "typed-deadline callback did not start within the hang guard"
        logical_now[0] = deadline + 0.01
    finally:
        release.set()
    worker.join(15)
    assert not worker.is_alive(), "pipeline did not finish within the hang guard"
    receipt = receipts[0]
    assert receipt["elapsed_seconds"] >= 1
    assert starts == ["a"], "typed read-budget exhaustion must stop further IO submissions"
    assert all(row["status"] == "deferred" for row in receipt["targets"].values())
    for row in receipt["targets"].values():
        assert row["deferred_reason"] == "deadline_exhausted"
        evidence = row["resource_evidence"]
        assert evidence["type"] == "round_deadline_exhausted"
        assert evidence["source_target"] == "a"
        assert evidence["reported_deadline"] == deadline
        assert evidence["remaining_seconds"] < 0
    result = validate_stop(path, ["a", "b", "c"], now=lambda: deadline + 0.001)
    assert result["valid"]


def test_typed_deadline_error_from_act_remains_uncertain(tmp_path):
    path = tmp_path / "pipeline.json"
    deadline = time.time() + 3
    cb = callbacks(
        act=lambda _target, _context: (_ for _ in ()).throw(
            FakeRoundDeadlineExhausted(deadline, 0.5)
        )
    )
    receipt = run_pipeline(
        path,
        ["a"],
        **cb,
        capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
        deadline=deadline,
        round_deadline_exception=FakeRoundDeadlineExhausted,
    )
    assert receipt["targets"]["a"]["status"] == "uncertain"
    assert receipt["targets"]["a"]["deferred_next_trigger"] == "reconcile_before_retry"
    assert not validate_stop(path, ["a"])["valid"]


def test_typed_github_capacity_deferral_is_validated_read_only_prepare_boundary(tmp_path):
    path = tmp_path / "pipeline.json"
    observed_at = time.time()
    reset_in = 4200
    reset_at = observed_at + reset_in
    throttle = {
        "resource": "core",
        "state": "shed",
        "action": "defer",
        "remaining": 50,
        "limit": 5000,
        "reset_in_s": reset_in,
        "source": "call",
        "reason": "50/5000 remaining; defer to reset",
    }
    calls = []
    cb = callbacks()

    def prepare(target, _context):
        calls.append(target)
        raise FakeRoundCapacityDeferred(
            resource="core",
            throttle=throttle,
            observed_at=observed_at,
            reset_at_epoch=reset_at,
        )

    cb["prepare"] = prepare
    receipt = run_pipeline(
        path,
        ["a"],
        **cb,
        capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
        deadline=time.time() + 3,
        round_capacity_exception=FakeRoundCapacityDeferred,
    )
    row = receipt["targets"]["a"]
    assert calls == ["a"]
    assert row["status"] == "deferred"
    assert row["deferred_reason"] == "github_capacity"
    assert row["resource_evidence"]["github_capacity"]["throttle"] == throttle
    assert row["deferred_next_trigger"] == f"retry_after_github_reset:{reset_at:.3f}"
    assert validate_stop(path, ["a"])["valid"]


def test_typed_github_capacity_error_from_act_is_uncertain(tmp_path):
    path = tmp_path / "pipeline.json"
    observed_at = time.time()
    throttle = {
        "resource": "core",
        "state": "shed",
        "action": "defer",
        "remaining": 50,
        "limit": 5000,
        "reset_in_s": 4200,
        "source": "call",
        "reason": "reserve reached",
    }
    cb = callbacks(
        act=lambda _target, _context: (_ for _ in ()).throw(
            FakeRoundCapacityDeferred(
                resource="core",
                throttle=throttle,
                observed_at=observed_at,
                reset_at_epoch=observed_at + 4200,
            )
        )
    )
    receipt = run_pipeline(
        path,
        ["a"],
        **cb,
        capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
        deadline=time.time() + 2,
        round_capacity_exception=FakeRoundCapacityDeferred,
    )
    assert receipt["targets"]["a"]["status"] == "uncertain"
    assert receipt["targets"]["a"]["deferred_next_trigger"] == "reconcile_before_retry"
    assert not validate_stop(path, ["a"])["valid"]


def test_stale_capacity_proof_validates_historical_stop_then_rechecks(tmp_path):
    path = tmp_path / "pipeline.json"
    observed_at = time.time()
    reset_in = 4200
    reset_at = observed_at + reset_in
    throttle = {
        "resource": "core",
        "state": "shed",
        "action": "defer",
        "remaining": 50,
        "limit": 5000,
        "reset_in_s": reset_in,
        "source": "call",
        "reason": "reserve reached",
    }
    first_callbacks = callbacks()
    first_callbacks["prepare"] = lambda *_: (_ for _ in ()).throw(
        FakeRoundCapacityDeferred(
            resource="core",
            throttle=throttle,
            observed_at=observed_at,
            reset_at_epoch=reset_at,
        )
    )
    first = run_pipeline(
        path,
        ["a"],
        **first_callbacks,
        capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
        deadline=time.time() + 3,
        round_capacity_exception=FakeRoundCapacityDeferred,
    )

    # Simulate a cadence after one hour. The reset proof was valid when the
    # first receipt completed, but is deliberately too old for current admission.
    old_observed_at = time.time() - 3600
    old_reset_at = old_observed_at + reset_in
    doc = json.loads(path.read_text())
    prior = doc["runs"][first["run_id"]]
    prior["created"] = old_observed_at - 10
    prior["deadline"] = old_observed_at + 60
    prior["receipt_summary"]["finished_at"] = old_observed_at + 5
    row = prior["targets"]["a"]
    capacity = row["resource_evidence"]["github_capacity"]
    capacity["observed_at"] = old_observed_at
    capacity["reset_at_epoch"] = old_reset_at
    capacity["throttle"]["reset_in_s"] = reset_in
    row["deferred_next_trigger"] = f"retry_after_github_reset:{old_reset_at:.3f}"
    doc["carry"]["a"]["resource_evidence"] = row["resource_evidence"]
    doc["carry"]["a"]["deferred_next_trigger"] = row["deferred_next_trigger"]
    path.write_text(json.dumps(doc))
    assert not validate_stop(path, ["a"])["valid"], "stale proof cannot authorize current stop"

    prepared = []
    next_callbacks = callbacks()
    next_callbacks["prepare"] = lambda target, _ctx: prepared.append(target) or {
        "revision": "fresh-revision",
        "identity": target,
    }
    resumed = run_pipeline(
        path,
        ["a"],
        **next_callbacks,
        capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
        deadline=time.time() + 3,
    )
    assert prepared == ["a"]
    assert resumed["targets"]["a"]["status"] == "completed"


def test_ambiguous_action_is_uncertain_and_never_replayed(tmp_path):
    calls = []
    cb = callbacks(
        act=lambda t, c: calls.append(t) or (_ for _ in ()).throw(RuntimeError("lost response"))
    )
    first = run(tmp_path / "state.json", ["a"], cb)
    second = run(tmp_path / "state.json", ["a"], cb)
    assert first["targets"]["a"]["status"] == "uncertain"
    assert second["targets"]["a"]["status"] == "uncertain"
    assert calls == ["a"]


def test_ambiguous_action_retries_only_after_reconcile_proves_not_started(tmp_path):
    calls, checks = [], []
    cb = callbacks(
        act=lambda t, c: calls.append(t)
        or (
            {"status": "completed", "target_complete": True}
            if len(calls) > 1
            else (_ for _ in ()).throw(RuntimeError("lost"))
        )
    )
    receipt = run_pipeline(
        tmp_path / "state.json",
        ["a"],
        **cb,
        reconcile=lambda t, c: checks.append(t) or {"outcome": "confirmed_not_started"},
        capacity_fn=lambda: {"io": 2, "low": 2, "exception": 1},
        deadline=time.time() + 2,
    )
    assert calls == ["a", "a"] and checks == ["a"]
    assert receipt["targets"]["a"]["status"] == "completed"


def test_crash_recovery_reconciles_durable_running_action(tmp_path):
    path = tmp_path / "state.json"
    run(path, ["a"])
    doc = json.loads(path.read_text())
    run_id = next(iter(doc["runs"]))
    doc["runs"][run_id]["deadline"] = time.time() + 3
    row = doc["runs"][run_id]["targets"]["a"]
    row.update(status="running", stage="act", work_key="lost-result")
    path.write_text(json.dumps(doc))
    acts, reconciles = [], []
    cb = callbacks(
        act=lambda t, c: acts.append(t) or {"status": "completed", "target_complete": True}
    )
    receipt = run_pipeline(
        path,
        ["a"],
        **cb,
        reconcile=lambda t, c: reconciles.append(t)
        or {"outcome": "confirmed_completed", "target_complete": True},
        capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
        deadline=time.time() + 4,
    )
    assert reconciles == ["a"] and not acts
    assert receipt["targets"]["a"]["status"] == "completed"


def test_interpret_error_does_not_authorize_action(tmp_path):
    acted = []
    cb = callbacks(
        interpret=lambda t, c: (_ for _ in ()).throw(RuntimeError("bad")),
        act=lambda t, c: acted.append(t) or {"status": "completed", "target_complete": True},
    )
    receipt = run(tmp_path / "state.json", ["a"], cb)
    assert not acted
    assert receipt["targets"]["a"]["status"] == "failed"


def test_failed_readonly_target_is_re_admitted_and_prior_stop_audited(tmp_path):
    path = tmp_path / "state.json"
    first = run(
        path,
        ["a"],
        callbacks(interpret=lambda *_: (_ for _ in ()).throw(RuntimeError("transient read"))),
    )
    assert first["targets"]["a"]["status"] == "failed"

    second = run(path, ["a"])
    assert second["targets"]["a"]["status"] == "completed"
    state = json.loads(path.read_text())
    new_run = state["runs"][second["run_id"]]
    assert new_run["prior_stop"]["valid"] is False
    assert any(
        reason == "unresolved_pipeline_work:a:failed" for reason in new_run["prior_stop"]["reasons"]
    )


def test_confirmed_parked_action_can_reprepare_but_unknown_action_cannot(tmp_path):
    path = tmp_path / "parked.json"
    first = run(path, ["a"])
    state = json.loads(path.read_text())
    row = state["runs"][first["run_id"]]["targets"]["a"]
    row.update(
        status="parked",
        stage="act",
        progress_key="known-progress",
        known_completed_action=True,
        results={"act": {"status": "completed", "target_complete": False}},
    )
    path.write_text(json.dumps(state))

    second = run(path, ["a"])
    assert second["targets"]["a"]["status"] == "completed"

    unsafe_path = tmp_path / "unsafe.json"
    failed = run(
        unsafe_path,
        ["a"],
        callbacks(interpret=lambda *_: (_ for _ in ()).throw(RuntimeError("read failed"))),
    )
    state = json.loads(unsafe_path.read_text())
    row = state["runs"][failed["run_id"]]["targets"]["a"]
    row.update(status="failed", stage="act")
    path_state = state
    unsafe_path.write_text(json.dumps(path_state))
    blocked = run(unsafe_path, ["a"])
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "prior_run_has_no_valid_stop"


def test_unrelated_prior_accounting_error_still_blocks_reentry(tmp_path):
    path = tmp_path / "state.json"
    first = run(
        path,
        ["a"],
        callbacks(interpret=lambda *_: (_ for _ in ()).throw(RuntimeError("transient read"))),
    )
    state = json.loads(path.read_text())
    state["runs"][first["run_id"]]["assignment"] = ["a", "missing"]
    path.write_text(json.dumps(state))
    blocked = run(path, ["a"])
    assert blocked["status"] == "blocked"
    assert blocked["prior_stop"]["reasons"] == [
        "assignment_not_fully_accounted",
        "unresolved_pipeline_work:a:failed",
    ]


def test_run_lock_is_released_when_state_save_raises(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    original_save = closer_pipeline._save
    save_calls = 0

    def fail_receipt_save(*args, **kwargs):
        nonlocal save_calls
        save_calls += 1
        if save_calls == 5:
            raise OSError("simulated receipt write failure")
        return original_save(*args, **kwargs)

    monkeypatch.setattr(closer_pipeline, "_save", fail_receipt_save)
    with pytest.raises(OSError, match="simulated receipt write failure"):
        run(path, ["a"])
    assert save_calls == 5

    monkeypatch.setattr(closer_pipeline, "_save", original_save)
    receipt = run(path, ["a"], seconds=1)
    assert receipt["targets"]["a"]["status"] == "completed"


def test_reprepare_replaces_stale_revision_before_act(tmp_path):
    path = tmp_path / "state.json"
    cb = callbacks()
    run(path, ["a"], cb)
    doc = json.loads(path.read_text())
    run_id = next(iter(doc["runs"]))
    doc["runs"][run_id]["deadline"] = time.time() + 3
    item = doc["runs"][run_id]["targets"]["a"]
    item.update(
        status="queued",
        stage="prepare",
        revision="rev-old",
        results={"prepare": {"revision": "rev-old"}},
    )
    path.write_text(json.dumps(doc))
    contexts = []
    cb["prepare"] = lambda t, c: {"revision": "rev-current", "identity": t}
    cb["act"] = lambda t, c: contexts.append(c) or {"status": "completed", "target_complete": True}
    run(path, ["a"], cb)
    assert contexts[0]["revision"] == "rev-current"
    assert contexts[0]["work_key"]


def test_fair_completion_service_and_concurrent_lanes(tmp_path):
    active, maximum = 0, 0
    lock = threading.Lock()

    def prep(target, ctx):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return {"revision": target}

    cb = callbacks(act=lambda t, c: {"status": "completed", "target_complete": True})
    cb["prepare"] = prep
    receipt = run(
        tmp_path / "state.json",
        [f"t{i}" for i in range(5)],
        cb,
        max_io_workers=3,
        max_low_workers=2,
    )
    assert maximum == 2  # caller's fresh capacity, not thread ceiling
    assert all(x["status"] == "completed" for x in receipt["targets"].values())


def test_capacity_shape_fails_closed(tmp_path):
    cb = callbacks()
    receipt = run_pipeline(
        tmp_path / "state.json",
        ["a"],
        **cb,
        capacity_fn=lambda: {"io": -1, "low": 1, "exception": 1},
        deadline=time.time() + 0.05,
    )
    assert receipt["targets"]["a"]["status"] == "failed"
    assert receipt["metrics"]["capacity_samples"][0]["error"]
    assert not validate_stop(tmp_path / "state.json", ["a"])["valid"]


def test_missing_capacity_lane_is_unknown_not_measured_zero(tmp_path):
    path = tmp_path / "state.json"
    receipt = run_pipeline(
        path,
        ["a"],
        **callbacks(),
        capacity_fn=lambda: {"io": 1, "low": 1},
        deadline=time.time() + 10,
    )
    assert receipt["targets"]["a"]["status"] == "failed"
    assert "missing the exception lane" in receipt["metrics"]["capacity_samples"][0]["error"]
    assert not validate_stop(path, ["a"])["valid"]


def test_validator_binds_assignment_and_deadline(tmp_path):
    path = tmp_path / "pipeline.json"
    receipt = run(path, ["a"], seconds=0.08, capacity={"io": 0, "low": 0, "exception": 0})
    result = validate_stop(path, ["a"], now=lambda: receipt["deadline"] + 1)
    assert result["valid"] and result["continuation_required"]
    changed = validate_stop(path, ["b"], now=lambda: receipt["deadline"] + 1)
    assert not changed["valid"]


def test_validator_rejects_live_uncertain_action(tmp_path):
    path = tmp_path / "pipeline.json"
    cb = callbacks(act=lambda t, c: (_ for _ in ()).throw(RuntimeError("unknown")))
    receipt = run(path, ["a"], cb, seconds=2)
    assert receipt["targets"]["a"]["status"] == "uncertain"
    doc = json.loads(path.read_text())
    run_id = next(iter(doc["runs"]))
    doc["runs"][run_id]["deadline"] = time.time() + 30
    path.write_text(json.dumps(doc))
    result = validate_stop(path, ["a"])
    assert not result["valid"]
    assert any("actionable_pipeline_work" in reason for reason in result["reasons"])


def test_validator_allows_only_specific_pending_async_boundary(tmp_path):
    path = tmp_path / "pipeline.json"
    cb = callbacks(
        act=lambda t, c: {
            "status": "pending",
            "pending_kind": "ci",
            "next_trigger": "check_completion",
            "pending_evidence": {"check_run_id": 42},
        }
    )
    receipt = run(path, ["a"], cb)
    assert receipt["targets"]["a"]["status"] == "pending"
    assert validate_stop(path, ["a"])["valid"]
    doc = json.loads(path.read_text())
    run_id = receipt["run_id"]
    doc["runs"][run_id]["targets"]["a"].pop("pending_evidence")
    path.write_text(json.dumps(doc))
    assert not validate_stop(path, ["a"])["valid"]


def test_validator_rejects_internal_handoff_and_loop_limit_as_stop_boundaries(tmp_path):
    path = tmp_path / "pipeline.json"
    receipt = run(
        path,
        ["a"],
        callbacks(
            act=lambda t, c: {
                "status": "deferred",
                "reason": "coordinator_integration_required",
                "next_trigger": "later",
                "resource_evidence": {"detail": "handoff needed"},
            }
        ),
    )
    assert not validate_stop(path, ["a"])["valid"]
    doc = json.loads(path.read_text())
    doc["runs"][receipt["run_id"]]["targets"]["a"].update(
        status="parked", deferred_reason="transition_limit", deferred_next_trigger="fresh_round"
    )
    path.write_text(json.dumps(doc))
    assert not validate_stop(path, ["a"])["valid"]


def test_failed_low_provider_is_audited_deferral_not_exception_or_action(tmp_path):
    target = "o/r#89"
    evidence = {
        "provider_failure": {
            "profile_id": "gemini-low",
            "agent": "gemini",
            "run_id": "dispatch-run-89",
            "exit": 1,
            "category": "unsupported_model",
            "evidence_hash": "provider-failure-sha256",
        }
    }
    callbacks = {
        "prepare": lambda *_: {"revision": "r1", "identity": "i1"},
        "interpret": lambda *_: {
            "status": "deferred",
            "reason": "provider_failure",
            "next_trigger": "retry after observed low-profile failure with fresh route admission",
            "resource_evidence": evidence,
            "substantive_needs": [],
        },
        "exception": lambda *_: (_ for _ in ()).throw(
            AssertionError("transport failure is not substantive")
        ),
        "act": lambda *_: (_ for _ in ()).throw(
            AssertionError("deferred provider failure must not act")
        ),
    }
    receipt = run_pipeline(
        tmp_path / "pipeline.json",
        [target],
        **callbacks,
        capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
        deadline=time.time() + 10,
        max_io_workers=1,
        max_low_workers=1,
        max_exception_workers=1,
    )
    row = receipt["targets"][target]
    assert row["status"] == "deferred"
    assert row["deferred_reason"] == "provider_failure"
    assert row["resource_evidence"] == evidence
    assert validate_stop(tmp_path / "pipeline.json")["valid"]


@pytest.mark.parametrize("stage", ["act", "reconcile"])
@pytest.mark.parametrize("assigned", [True, False])
def test_expired_action_deadline_recovers_only_to_reconcile(tmp_path, stage, assigned):
    path = tmp_path / "pipeline.json"
    at = 1_800_000_000.0
    interrupted_round_fixture(path, at)
    state = closer_pipeline._load(path)
    prior = state["runs"]["interrupted"]
    target = "owner/repo#capacity-0"
    prior["targets"][target].update(
        status="deferred",
        stage=stage,
        deferred_reason="deadline_exhausted",
        deferred_next_trigger="next_round",
        resource_evidence={
            "type": "active_deadline",
            "deadline": at - 1000,
            "observed_at": at - 999,
        },
        results={"reconcile": {"outcome": "confirmed_not_started"}},
    )
    closer_pipeline._save(path, state)
    calls = []
    cb = callbacks(
        act=lambda name, _ctx: calls.append(("act", name))
        or {"status": "completed", "target_complete": True}
    )
    result = run(
        path,
        [target] if assigned else ["owner/repo#capacity-1"],
        cb,
        seconds=10,
        now=lambda: at,
        reconcile=lambda name, _ctx: calls.append(("reconcile", name)) or {"outcome": "unknown"},
    )
    assert result.get("status") != "blocked"
    assert ("act", target) not in calls
    assert (("reconcile", target) in calls) is assigned
    latest = next(iter(closer_pipeline._load(path)["runs"].values()))
    assert latest["prior_stop"]["valid"] is False
    audit = latest["invalid_stop_history"][-1]["targets"][target]
    assert audit["recovery"] == "reconcile_only"
    if not assigned:
        assert latest["targets"][target]["recovery_excluded"] is True


@pytest.mark.parametrize(
    "mutation", ["wrong_deadline", "early", "operation_result", "provider_unknown"]
)
def test_expired_action_deadline_requires_exact_no_result_boundary(tmp_path, mutation):
    path = tmp_path / "pipeline.json"
    at = 1_800_000_000.0
    interrupted_round_fixture(path, at)
    state = closer_pipeline._load(path)
    prior = state["runs"]["interrupted"]
    row = prior["targets"]["owner/repo#capacity-0"]
    row.update(
        status="deferred",
        stage="act",
        deferred_reason="deadline_exhausted",
        resource_evidence={
            "type": "active_deadline",
            "deadline": at - 1000,
            "observed_at": at - 999,
        },
    )
    if mutation == "wrong_deadline":
        row["resource_evidence"]["deadline"] -= 1
    elif mutation == "early":
        row["resource_evidence"]["observed_at"] = at - 1001
    elif mutation == "operation_result":
        row["results"]["act"] = {"status": "blocked"}
    else:
        row["provider_outcome_uncertain"] = {"unknown": True}
    closer_pipeline._save(path, state)
    stop = validate_stop(path, prior["assignment"], now=lambda: at)
    assert not closer_pipeline._expired_invalid_round_recovery(
        stop, prior, path, "interrupted", at
    )[0]
