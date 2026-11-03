"""Fatal control-plane exits release claims only after actual workers finish."""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_closer_queue import _snapshot

import closer_queue as queue


class WorkerInterrupted(BaseException):
    pass


class ClaimBook:
    def __init__(self):
        self.lock = threading.Lock()
        self.held = {}
        self.active = {}
        self.released = []
        self.premature = []

    def claim(self, target, owner):
        with self.lock:
            if target in self.held:
                return False
            self.held[target] = owner
            return True

    def release(self, target, owner):
        with self.lock:
            if any(target in claimed for claimed in self.active.values()):
                self.premature.append(target)
                return False
            assert self.held.pop(target) == owner
            self.released.append(target)
            return True

    def holder(self, target):
        with self.lock:
            return {"agent": self.held[target]} if target in self.held else None

    def start(self, target, operation):
        with self.lock:
            self.active[target] = operation["claim_targets"]

    def finish(self, target):
        with self.lock:
            self.active.pop(target)


def run(path, targets, execute, book, **kwargs):
    return queue.run_round(
        path,
        targets,
        lambda target: _snapshot("original-revision", "repair"),
        execute,
        deadline=time.time() + 30,
        receipt_reserve_seconds=0,
        min_action_seconds=0,
        max_passes=1,
        claim_fn=book.claim,
        holder_fn=book.holder,
        release_fn=book.release,
        **kwargs,
    )


@pytest.mark.parametrize("intent_write", [1, 2], ids=["intent-write", "event-write"])
def test_fatal_preinvoke_write_releases_claims_and_preserves_saved_intent(
    tmp_path, monkeypatch, intent_write
):
    path, target, book = tmp_path / "queue.json", "one/repo#1", ClaimBook()
    atomic = queue._atomic_write
    submitted_writes = []
    calls = []

    def failed(path, document):
        operation = document.get("items", {}).get(target, {}).get("operation") or {}
        if operation.get("status") == "submitted":
            submitted_writes.append(True)
            if len(submitted_writes) == intent_write:
                raise OSError("synthetic durable intent failure")
        return atomic(path, document)

    monkeypatch.setattr(queue, "_atomic_write", failed)
    with pytest.raises(OSError, match="durable intent failure"):
        run(path, [target], lambda *_: calls.append(True), book)
    assert calls == []
    assert book.held == {} and book.active == {} and book.premature == []
    assert Counter(book.released) == Counter({target: 1, "closer-repo:one/repo": 1})
    if intent_write == 2:
        saved = json.loads(path.read_bytes())["items"][target]["operation"]
        assert saved["status"] == "submitted" and saved["attempts"] == 1
        monkeypatch.setattr(queue, "_atomic_write", atomic)
        run(path, [target], lambda *_: pytest.fail("durable intent must not be retried"), book)
        retained = json.loads(path.read_bytes())["items"][target]["operation"]
        assert retained["id"] == saved["id"]
        assert retained["status"] == "uncertain" and retained["attempts"] == 1


@pytest.mark.parametrize("failure", ["result-write", "worker-interrupted"])
def test_fatal_exit_waits_for_other_worker_before_releasing_its_claims(
    tmp_path, monkeypatch, failure
):
    path, book = tmp_path / "queue.json", ClaimBook()
    first, second = "one/repo#1", "two/repo#2"
    second_started, finish_second = threading.Event(), threading.Event()
    second_done, fatal_seen = threading.Event(), threading.Event()
    calls, errors = [], []
    atomic = queue._atomic_write

    def failed(path, document):
        operation = document.get("items", {}).get(first, {}).get("operation") or {}
        if failure == "result-write" and operation.get("status") == "completed":
            fatal_seen.set()
            raise OSError("synthetic durable result failure")
        return atomic(path, document)

    monkeypatch.setattr(queue, "_atomic_write", failed)

    def execute(target, _action, _snapshot, operation):
        book.start(target, operation)
        calls.append(target)
        try:
            if target == second:
                second_started.set()
                assert finish_second.wait(5)
                return {"status": "completed"}
            assert second_started.wait(5)
            if failure == "worker-interrupted":
                fatal_seen.set()
                raise WorkerInterrupted("synthetic worker interruption")
            return {"status": "completed"}
        finally:
            book.finish(target)
            if target == second:
                second_done.set()

    def coordinate():
        try:
            run(path, [first, second], execute, book, max_workers=2)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=coordinate)
    thread.start()
    try:
        assert fatal_seen.wait(5)
        thread.join(0.05)
        assert thread.is_alive(), "coordinator escaped while another callback was active"
        assert not errors
        with book.lock:
            assert second in book.active
            assert second in book.held and "closer-repo:two/repo" in book.held
            assert second not in book.released and not book.premature
    finally:
        finish_second.set()
        thread.join(5)
        assert second_done.wait(5)
    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], OSError if failure == "result-write" else WorkerInterrupted)
    assert sorted(calls) == [first, second]
    assert book.held == {} and book.active == {} and book.premature == []
    assert all(count == 1 for count in Counter(book.released).values())
    saved = json.loads(path.read_bytes())["items"]
    original = {target: saved[target]["operation"]["id"] for target in (first, second)}
    assert all(
        saved[target]["operation"]["status"] in queue.PENDING_OPERATION_STATES
        for target in (first, second)
    )
    monkeypatch.setattr(queue, "_atomic_write", atomic)
    run(path, [first, second], lambda *_: pytest.fail("uncertain effects cannot be retried"), book)
    retained = json.loads(path.read_bytes())["items"]
    assert all(
        retained[target]["operation"]["id"] == original[target]
        and retained[target]["operation"]["status"] == "uncertain"
        and retained[target]["operation"]["attempts"] == 1
        for target in (first, second)
    )


def test_fatal_exit_cancels_queued_callback_without_granting_retry(tmp_path, monkeypatch):
    path, book = tmp_path / "queue.json", ClaimBook()
    first, second = "one/repo#1", "two/repo#2"
    queued, cancelled, release_thread = threading.Event(), threading.Event(), threading.Event()
    errors, calls = [], []
    atomic = queue._atomic_write

    class QueuedExecutor(ThreadPoolExecutor):
        def __init__(self, **_kwargs):
            # A deliberately narrower real executor leaves the second admitted
            # request queued, exercising shutdown cancellation deterministically.
            super().__init__(max_workers=1)
            self.count = 0

        def submit(self, fn, *args, **kwargs):
            self.count += 1
            future = super().submit(fn, *args, **kwargs)
            if self.count == 1:

                def hold_worker_after_result(_future):
                    assert release_thread.wait(5)

                future.add_done_callback(hold_worker_after_result)
            else:
                future.add_done_callback(
                    lambda value: cancelled.set() if value.cancelled() else None
                )
                queued.set()
            return future

    def failed(path, document):
        operation = document.get("items", {}).get(first, {}).get("operation") or {}
        if operation.get("status") == "completed":
            raise OSError("synthetic durable result failure")
        return atomic(path, document)

    def execute(target, _action, _snapshot, operation):
        book.start(target, operation)
        calls.append(target)
        try:
            assert target == first, "cancelled queued callback must not execute"
            assert queued.wait(5)
            return {"status": "completed"}
        finally:
            book.finish(target)

    def coordinate():
        try:
            run(path, [first, second], execute, book, max_workers=2)
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(queue, "ThreadPoolExecutor", QueuedExecutor)
    monkeypatch.setattr(queue, "_atomic_write", failed)
    thread = threading.Thread(target=coordinate)
    thread.start()
    try:
        assert cancelled.wait(5), "fatal unwind did not cancel the queued callback"
        assert thread.is_alive() and not errors
        with book.lock:
            assert second in book.held and not book.premature
    finally:
        release_thread.set()
        thread.join(5)
    assert not thread.is_alive() and len(errors) == 1 and isinstance(errors[0], OSError)
    assert calls == [first] and book.held == {} and book.premature == []
    saved = json.loads(path.read_bytes())["items"][second]["operation"]
    assert saved["status"] == "submitted" and saved["attempts"] == 1
    monkeypatch.setattr(queue, "_atomic_write", atomic)
    run(
        path,
        [second],
        lambda *_: pytest.fail("cancelled durable intent requires reconciliation"),
        book,
    )
    retained = json.loads(path.read_bytes())["items"][second]["operation"]
    assert retained["id"] == saved["id"] and retained["status"] == "uncertain"
    assert retained["attempts"] == 1


def test_existing_release_failure_is_reported_without_hidden_cleanup_retry(tmp_path):
    path, book = tmp_path / "queue.json", ClaimBook()
    target = "one/repo#1"
    attempts = []
    release = book.release

    def failed_once(name, owner):
        attempts.append(name)
        return False if name == target else release(name, owner)

    book.release = failed_once
    run(path, [target], lambda *_: {"status": "blocked"}, book)
    row = json.loads(path.read_bytes())["items"][target]
    assert row["claim_release_failures"] == [target]
    assert attempts.count(target) == 1
    assert target in book.held and "closer-repo:one/repo" not in book.held
