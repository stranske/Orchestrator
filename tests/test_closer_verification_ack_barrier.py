"""Actual queue/no-start producer paths; never a remote mutation."""

import copy
import json
from types import SimpleNamespace

import pytest
from test_closer_queue import _run

import closer_dispatcher as dispatcher
import closer_queue as queue

TARGET = "offline/repo#1"


def world(tmp_path, monkeypatch, mode):
    snapshot = {
        "target": TARGET,
        "pr_target": TARGET,
        "head": "a" * 40,
        "merge": "b" * 40,
        "revision": "r1",
        "action": "post_merge_verify",
        "eligible": True,
        "evidence": {},
        "operation_binding": {"pr_target": TARGET, "head": "a" * 40, "merge": "b" * 40},
        "claim_targets": [TARGET],
        "repo_keys": ["offline/repo"],
    }
    fresh = copy.deepcopy(snapshot)
    if mode == "stale":
        fresh["revision"] = "r2"

    def read(_target):
        if mode == "not_started":
            raise TimeoutError("synthetic read-only freshness loss")
        return copy.deepcopy(fresh)

    backend = dispatcher.Closer(
        SimpleNamespace(snapshot=read, deadline=float("inf")),
        state_path=tmp_path / "queue.json",
        apply=mode != "deferred",
    )
    monkeypatch.setattr(backend, "verification_snapshot", lambda value: value)
    monkeypatch.setattr(
        backend,
        "select",
        lambda value: (
            ("wait", "synthetic selected hold", False, "fresh evidence")
            if mode == "blocked_select"
            else ("post_merge_verify", "ready", True, "")
        ),
    )
    if mode == "blocked_assignment":
        backend.assignment_guard_fn = lambda _: {"reason": "synthetic changed assignment"}
    monkeypatch.setattr(
        backend, "prepare_verify", lambda *a: pytest.fail("pre-write branch entered prepare_verify")
    )
    return backend, snapshot


@pytest.mark.parametrize("mode", ["stale", "deferred", "blocked_select", "blocked_assignment"])
def test_actual_pre_prepare_failure_is_not_a_verification_ack_barrier(tmp_path, monkeypatch, mode):
    backend, snapshot = world(tmp_path, monkeypatch, mode)
    _run(
        backend.state_path,
        [TARGET],
        lambda _: copy.deepcopy(snapshot),
        backend.execute,
        max_passes=1,
    )
    op = queue._read_state(backend.state_path)["items"][TARGET]["operation"]
    assert op["status"] == "failed"
    assert op["outcome"] in {"stale", "blocked", "deferred"}
    assert backend._unacknowledged_verification_request(snapshot) is None


def test_actual_no_start_then_changed_revision_abandoned_history_does_not_fence(
    tmp_path, monkeypatch
):
    backend, snapshot = world(tmp_path, monkeypatch, "not_started")
    _run(
        backend.state_path,
        [TARGET],
        lambda _: copy.deepcopy(snapshot),
        backend.execute,
        max_passes=1,
    )
    assert (
        queue._read_state(backend.state_path)["items"][TARGET]["operation"]["status"]
        == "confirmed_not_started"
    )
    changed = {**snapshot, "revision": "r2", "action": "wait", "eligible": False}
    _run(
        backend.state_path,
        [TARGET],
        lambda _: copy.deepcopy(changed),
        backend.execute,
        max_passes=1,
    )
    history = queue._read_state(backend.state_path)["items"][TARGET]["operation_history"]
    assert history[-1]["status"] == "abandoned"
    assert backend._unacknowledged_verification_request(snapshot) is None


@pytest.mark.parametrize(
    "status", ["abandoned", "failed", "submitted", "running", "uncertain", "completed"]
)
def test_unproved_abandoned_failed_and_possible_submissions_remain_fenced(
    tmp_path, monkeypatch, status
):
    backend, snapshot = world(tmp_path, monkeypatch, "stale")
    _run(
        backend.state_path,
        [TARGET],
        lambda _: copy.deepcopy(snapshot),
        backend.execute,
        max_passes=1,
    )
    state = queue._read_state(backend.state_path)
    op = state["items"][TARGET]["operation"]
    op.update(
        status=status, outcome="blocked", result={"reason": "unproved or lost original result"}
    )
    backend.state_path.write_text(json.dumps(state))
    assert backend._unacknowledged_verification_request(snapshot)["operation_id"] == op["id"]


@pytest.mark.parametrize(
    "marker,value",
    [
        ("verification_post_accepted", None),
        ("verification_request_basis", "a" * 64),
        ("core_request_correlation", {}),
        ("native_result", {}),
        ("mutation_intent", {}),
        ("effect_started", True),
        ("post_started", True),
        ("outcome_uncertain", True),
        ("native_binding", {}),
    ],
)
def test_bound_no_start_proof_never_overrides_conflicting_effect_evidence(
    tmp_path, monkeypatch, marker, value
):
    backend, snapshot = world(tmp_path, monkeypatch, "stale")
    _run(
        backend.state_path,
        [TARGET],
        lambda _: copy.deepcopy(snapshot),
        backend.execute,
        max_passes=1,
    )
    state = queue._read_state(backend.state_path)
    op = state["items"][TARGET]["operation"]
    assert op["result"]["verification_no_start"]["phase"] == "before_prepare_verify"
    op["result"][marker] = value
    backend.state_path.write_text(json.dumps(state))
    assert backend._unacknowledged_verification_request(snapshot)["operation_id"] == op["id"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation_id", "foreign"),
        ("target", "foreign/repo#1"),
        ("action", "initiate_verify"),
        ("revision", "stale"),
        ("binding_sha256", "f" * 64),
        ("phase", "after_submit"),
        ("result_status", "deferred"),
        ("post_started", 0),
        ("extra", False),
    ],
)
def test_wrong_partial_or_nominal_false_no_start_proof_cannot_clear_barrier(
    tmp_path, monkeypatch, field, value
):
    backend, snapshot = world(tmp_path, monkeypatch, "stale")
    _run(
        backend.state_path,
        [TARGET],
        lambda _: copy.deepcopy(snapshot),
        backend.execute,
        max_passes=1,
    )
    state = queue._read_state(backend.state_path)
    op = state["items"][TARGET]["operation"]
    op["result"]["verification_no_start"][field] = value
    backend.state_path.write_text(json.dumps(state))
    assert backend._unacknowledged_verification_request(snapshot)["operation_id"] == op["id"]


@pytest.mark.parametrize("accepted", [True, False])
def test_actual_post_acceptance_or_lost_ack_never_allows_second_request(
    tmp_path, monkeypatch, accepted
):
    backend, snapshot = world(tmp_path, monkeypatch, "post")
    calls = []

    def api(endpoint, **kwargs):
        if kwargs.get("method") == "POST":
            calls.append(endpoint)
            if not accepted:
                raise TimeoutError("synthetic lost POST acknowledgment")
            return {"accepted": True}
        assert endpoint == "repos/offline/repo"
        return {"default_branch": "main"}

    backend.gh.api = api
    monkeypatch.setattr(
        backend, "prepare_verify", dispatcher.Closer.prepare_verify.__get__(backend)
    )
    monkeypatch.setattr(backend, "_verification_preflight", lambda *a: None)
    _run(
        backend.state_path,
        [TARGET],
        lambda _: copy.deepcopy(snapshot),
        backend.execute,
        max_passes=1,
    )
    operation = queue._read_state(backend.state_path)["items"][TARGET]["operation"]
    assert len(calls) == 1
    assert "verification_no_start" not in operation.get("result", {})
    assert operation["status"] == ("completed" if accepted else "uncertain")
    again = backend.prepare_verify(TARGET, snapshot)
    assert again["status"] == "not_started" and "mutation_kind" not in again
    assert len(calls) == 1
