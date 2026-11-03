"""Actual scheduler mechanics; no source IO/installed/helper authority positive."""

import threading
import time

import pytest
from test_closer_queue import _run, _snapshot

import closer_coding_publication as publication
import closer_coding_publication_io as publication_io
import closer_queue as queue


def test_publication_is_a_deliberate_mutating_action():
    assert publication_io.ACTION in queue.MUTATING_ACTIONS
    assert publication_io.ACTION not in queue.READ_ONLY_ACTIONS
    assert publication_io.ACTION not in queue.TERMINAL_ACTIONS


def test_actual_queue_serializes_publication_with_same_repository_write(tmp_path):
    actions = {"acme/repo#9": publication_io.ACTION, "acme/repo#10": "repair"}
    active = []
    calls = []
    lock = threading.Lock()

    def observe(target):
        return _snapshot("r1", actions[target], eligible=actions[target] != "complete")

    def execute(target, action, snapshot, operation):
        with lock:
            assert not active
            active.append(target)
            calls.append((target, action, operation["status"]))
        time.sleep(0.01)
        with lock:
            active.remove(target)
            actions[target] = "complete"
        return {"status": "completed", "advanced": True}

    receipt = _run(
        tmp_path / "new-publication-queue.json", list(actions), observe, execute, max_workers=2
    )
    assert len(calls) == 2
    assert all(state == "submitted" for _, _, state in calls)
    assert receipt["valid"] and receipt["stop_valid"]
    assert set(receipt["accounted_targets"]) == set(actions)
    assert receipt["eligible_remaining"] == []


@pytest.mark.parametrize("scope", [None, {}, True, object()])
def test_unissued_scope_refuses_before_queue_files(tmp_path, scope, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(publication.PublicationHeld, match="actual source publication scope"):
        publication_io.run_callback_round(scope)
    assert not list(tmp_path.iterdir())


def test_private_scope_constructor_and_detached_instance_never_issue():
    with pytest.raises(TypeError, match="source-issued"):
        publication_io._PublicationScope()
    detached = object.__new__(publication_io._PublicationScope)
    with pytest.raises(publication.PublicationHeld, match="actual source publication IO lifetime"):
        detached.check()


def test_unscoped_completion_and_callback_cannot_claim_results():
    with pytest.raises(publication.PublicationHeld):
        publication_io._execute({}, "acme/repo#9", publication_io.ACTION, {}, {})
    with pytest.raises(TypeError, match="source-issued"):
        publication_io._ConfirmedPublication({"published": True})


@pytest.mark.parametrize("detached", [None, False, {}, object()])
def test_inline_selector_rejects_caller_options_before_queue_write(tmp_path, detached):
    # None is the deliberate default; it is not passed as a selection here.
    selected = object() if detached is None else detached
    path = tmp_path / "never-created.json"
    with pytest.raises(queue.QueueStateError, match="actual same-thread publication source"):
        queue.run_round(
            path,
            ["acme/repo#9"],
            lambda _: {},
            lambda *_: {},
            deadline=time.time() + 10,
            _publication_inline_source=selected,
        )
    assert not path.exists()
    assert not list(tmp_path.iterdir())


def test_detached_exact_scope_cannot_select_inline_before_queue_write(tmp_path):
    detached = object.__new__(publication_io._PublicationScope)
    with pytest.raises(queue.QueueStateError, match="actual same-thread publication source"):
        queue.run_round(
            tmp_path / "never.json",
            ["acme/repo#9"],
            lambda _: {},
            lambda *_: {},
            deadline=time.time() + 10,
            _publication_inline_source=detached,
        )
    assert not list(tmp_path.iterdir())


def test_inline_mechanics_preserve_real_invocation_and_original_thread(tmp_path):
    owner = threading.get_ident()
    operation = {"id": "owned", "status": "submitted", "deadline": time.time() + 5}
    seen = []

    def execute(target, action, snapshot, actual):
        seen.append((threading.get_ident(), queue._currently_invoking(target, actual)))
        assert actual is operation
        return {"status": "completed"}

    result, invoked_at = queue._inline_before_cutoff(
        execute,
        "acme/repo#9",
        publication_io.ACTION,
        {},
        operation,
        time.time,
        None,
        tmp_path / "mechanics-only.json",
    ).result()
    assert result == {"status": "completed"}
    assert type(invoked_at) is float
    assert seen == [(owner, True)]
    assert not queue._currently_invoking("acme/repo#9", operation)


@pytest.mark.parametrize("error", [RuntimeError("fixed"), KeyboardInterrupt(), SystemExit(9)])
def test_inline_future_preserves_original_exception_and_clears_invocation(error):
    operation = {"id": "owned", "status": "submitted", "deadline": time.time() + 5}

    def execute(*_):
        raise error

    future = queue._inline_before_cutoff(
        execute,
        "acme/repo#9",
        publication_io.ACTION,
        {},
        operation,
        time.time,
    )
    with pytest.raises(type(error)) as caught:
        future.result()
    assert caught.value is error
    assert not queue._currently_invoking("acme/repo#9", operation)


def test_inline_cutoff_never_invokes_after_original_deadline():
    calls = []
    assert queue._inline_before_cutoff(
        lambda *_: calls.append(True),
        "acme/repo#9",
        publication_io.ACTION,
        {},
        {"deadline": 4},
        lambda: 4,
    ).result() == (None, None)
    assert calls == []


def test_ordinary_default_still_runs_callback_on_executor_thread(tmp_path):
    owner = threading.get_ident()
    calls = []
    current = ["repair"]

    def observe(_):
        return _snapshot("r1", current[0], eligible=current[0] != "complete")

    def execute(*_):
        calls.append(threading.get_ident())
        current[0] = "complete"
        return {"status": "completed", "advanced": True}

    receipt = _run(tmp_path / "ordinary.json", ["acme/repo#9"], observe, execute)
    assert receipt["valid"]
    assert calls and all(thread != owner for thread in calls)


def test_combined_coding_and_publication_inline_options_refuse_before_queue_or_callbacks(tmp_path):
    import closer_queue as queue

    calls = []
    path = tmp_path / "not-issued.json"
    with pytest.raises(queue.QueueStateError, match="cannot be combined"):
        queue.run_round(
            path,
            ["acme/repo#9"],
            lambda *_: calls.append("observe"),
            lambda *_: calls.append("execute"),
            deadline=None,
            _inline_source={},
            _publication_inline_source={},
        )
    assert calls == [] and not path.exists()
