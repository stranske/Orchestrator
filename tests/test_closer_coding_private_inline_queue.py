"""Real queue scheduling, synthetic rejection-only root; no authority issued."""

import inspect
import json
import os
import threading
import time
from pathlib import Path

import pytest
from test_closer_queue import _frontier, _run, _snapshot

import closer_coding_worker as worker
import closer_queue as queue
import closer_resources as resources


def test_real_queue_single_worker_crosses_original_worker_thread(tmp_path):
    thread = threading.get_ident()
    # Only the unchanged first rejection guard is exercised. This detached
    # object cannot pass the later source/frame/installation predicates.
    root = worker._Root(
        worker._SEAL, inspect.currentframe(), thread, os.getpid(), None, None, {}, b"{}", {}
    )
    observed = []
    executed = []

    def observe(target):
        observed.append(threading.get_ident())
        return _snapshot("r1", "correction_coding")

    def execute(target, action, snapshot, operation):
        executed.append(threading.get_ident())
        root.check()
        raise AssertionError("source guard must refuse before this line")

    path = tmp_path / "new-inert-coding-queue.json"
    _run(
        path,
        ["acme/repo#9"],
        observe,
        execute,
        owner="inert-owner",
        max_workers=1,
        max_passes=1,
        max_operation_retries=0,
    )
    state = json.loads(path.read_text())
    operation = state["items"]["acme/repo#9"]["operation"]
    assert observed and all(value == thread for value in observed)
    assert len(executed) == 1 and executed[0] != thread
    assert operation["status"] == "uncertain"
    assert operation["error_type"] == "WorkerHeld"
    assert operation["attempts"] == 1


@pytest.mark.parametrize("selected", [True, {}, object()])
def test_detached_inline_selection_refuses_before_queue_writes(tmp_path, selected):
    path = tmp_path / "must-not-exist.json"
    calls = []
    with pytest.raises(queue.QueueStateError, match="actual single coding source queue required"):
        queue.run_round(
            path,
            ["acme/repo#9"],
            lambda _: calls.append("observe"),
            lambda *a: calls.append("execute"),
            deadline=time.time() + 10,
            max_workers=1,
            max_passes=1,
            max_operation_retries=0,
            _inline_source=selected,
        )
    assert calls == []
    assert not path.exists() and not Path(str(path) + ".lock").exists()


def test_forged_typed_root_cannot_select_inline(tmp_path):
    root = worker._Root(
        worker._SEAL,
        inspect.currentframe(),
        threading.get_ident(),
        os.getpid(),
        None,
        None,
        {},
        b"{}",
        {},
    )
    path = tmp_path / "must-not-exist.json"
    with pytest.raises(queue.QueueStateError):
        queue.run_round(
            path,
            ["acme/repo#9"],
            lambda _: {},
            lambda *a: {},
            deadline=time.time() + 10,
            max_workers=1,
            max_passes=1,
            max_operation_retries=0,
            _inline_source=root,
        )
    assert not path.exists()


def test_inline_wrapper_preserves_thread_and_real_callback_tls(tmp_path):
    owner = threading.get_ident()
    target = "acme/repo#9"
    op = {"status": "submitted", "deadline": time.time() + 10}

    def execute(which, action, snapshot, operation):
        assert threading.get_ident() == owner
        assert queue._currently_invoking(target, operation)
        return {"status": "completed"}

    future = queue._inline_before_cutoff(
        execute, target, "correction_coding", {}, op, time.time, None, tmp_path / "queue.json"
    )
    result, invoked = future.result()
    assert future.done() and result == {"status": "completed"}
    assert isinstance(invoked, float)
    assert not queue._currently_invoking(target, op)


def test_inline_wrapper_preserves_finite_resource_context(tmp_path):
    target = "acme/repo#9"
    frontier = _frontier([target], frontier_id="new-inline", actions={target: "correction_coding"})
    deadline = time.time() + 10
    op = {"status": "submitted", "deadline": deadline, "finite_frontier": frontier}
    owner = threading.get_ident()
    with resources.inventory_request(deadline):
        before = resources.finite_request_remaining()
        context = resources.capture_finite_execution_context(target, frontier)

        def execute(which, action, snapshot, operation):
            assert threading.get_ident() == owner
            assert queue._currently_invoking(target, operation)
            assert 0 < resources.finite_request_remaining() <= before
            return {"status": "completed"}

        result, _ = queue._inline_before_cutoff(
            execute,
            target,
            "correction_coding",
            {},
            op,
            time.time,
            context,
            tmp_path / "queue.json",
        ).result()
        assert result["status"] == "completed"
        assert 0 < resources.finite_request_remaining() <= before


def test_inline_wrapper_expired_cutoff_has_zero_callback():
    calls = []
    future = queue._inline_before_cutoff(
        lambda *a: calls.append(a),
        "acme/repo#9",
        "correction_coding",
        {},
        {"status": "submitted", "deadline": 9},
        lambda: 10,
    )
    assert future.result() == (None, None)
    assert calls == []


def test_inline_wrapper_retains_exception_without_tls_leak():
    op = {"status": "submitted", "deadline": time.time() + 10}
    error = worker.WorkerHeld("fixed synthetic refusal")

    def execute(*args):
        assert queue._currently_invoking("acme/repo#9", op)
        raise error

    future = queue._inline_before_cutoff(
        execute, "acme/repo#9", "correction_coding", {}, op, time.time
    )
    assert future.exception() is error
    assert not queue._currently_invoking("acme/repo#9", op)
