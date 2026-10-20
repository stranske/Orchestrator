"""Small offline executor handoff controls; no Core copies or external requests."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import closer_frontier_policy as policy
import closer_queue as cq
import closer_resources as cr

TARGET = "owner/repo#1"


def frontier(frontier_id="context-handoff", target=TARGET):
    return policy.make_frontier(
        [target],
        owner="context-owner",
        frontier_id=frontier_id,
        bindings={
            target: {
                "revision": "r1",
                "action_id": policy.action_identity(
                    target, "r1", "collect_evidence", {"pr_target": target, "source_targets": []}
                ),
            }
        },
    )


def slot(root, f):
    return cr.pool_slot(
        root,
        "io",
        1,
        deadline=time.time() + 2,
        finite_frontier=True,
        reservation={
            "reservation_id": "context-reservation",
            "target": f["targets"][0],
            "frontier_id": f["frontier_id"],
            "agent": "closer",
            "profile_id": "io",
        },
    )


def operation(f, seconds=1):
    return {"deadline": time.time() + seconds, "finite_frontier": f}


def invoke(context, f, callback, seconds=1):
    return cq._invoke_before_cutoff(
        callback, TARGET, "collect_evidence", {}, operation(f, seconds), time.time, context
    )


def test_actual_queue_worker_inherits_original_window_and_held_pool(tmp_path):
    f = frontier()
    root = tmp_path / "pool"
    seen = []
    parent_thread = threading.get_ident()
    action = ["collect_evidence"]

    def observe(target):
        return {"revision": "r1", "action": action[0], "eligible": True}

    def execute(target, selected, snapshot, op):
        seen.append(
            {
                "thread": threading.get_ident(),
                "remaining": cr.finite_request_remaining(),
                "binding": cr.finite_pool_current_binding(),
                "item": cr._pool_slots()[0] if cr._pool_slots() else None,
            }
        )
        cr.finite_pool_no_start()
        action[0] = "complete"
        return {"status": "completed", "advanced": True}

    with slot(root, f), cr.inventory_request(time.time() + 0.7):
        parent_item = cr._pool_slots()[0]
        receipt = cq.run_round(
            tmp_path / "queue.json",
            [TARGET],
            observe,
            execute,
            owner="context-owner",
            deadline=None,
            finite_frontier=f,
            frontier_target=TARGET,
            request_timeout_seconds=1,
            receipt_reserve_seconds=0,
            min_action_seconds=0,
            max_operation_retries=0,
            claim_fn=lambda *_: True,
            release_fn=lambda *_: True,
            holder_fn=lambda *_: None,
        )
        assert seen and seen[0]["remaining"] is not None
        assert 0 < seen[0]["remaining"] < 0.7
        assert seen[0]["thread"] != parent_thread
        assert seen[0]["binding"] == {"frontier_id": f["frontier_id"], "target": TARGET}
        assert seen[0]["item"] is parent_item
        assert parent_item["record"]["status"] == "resolved"
        assert not parent_item["handle"].closed
        assert receipt["valid"]
        stored = json.loads((root / "pools/io/slot-0.json").read_text())
        assert stored["resolution"] == "trusted_no_spawn"


def test_public_finite_queue_establishes_operation_window_without_pool(tmp_path):
    f = frontier()
    seen = []
    action = ["collect_evidence"]

    def execute(*args):
        seen.append((cr.finite_request_remaining(), cr.finite_pool_current_binding()))
        action[0] = "complete"
        return {"status": "completed", "advanced": True}

    cq.run_round(
        tmp_path / "public.json",
        [TARGET],
        lambda *_: {"revision": "r1", "action": action[0], "eligible": True},
        execute,
        owner="context-owner",
        deadline=None,
        finite_frontier=f,
        frontier_target=TARGET,
        request_timeout_seconds=0.7,
        receipt_reserve_seconds=0,
        min_action_seconds=0,
        max_operation_retries=0,
        claim_fn=lambda *_: True,
        release_fn=lambda *_: True,
        holder_fn=lambda *_: None,
    )
    assert len(seen) == 1 and 0 < seen[0][0] <= 0.7 and seen[0][1] is None
    assert cr.finite_request_remaining() is None


def test_expired_inherited_window_prevents_callback_even_with_later_operation():
    f = frontier()
    calls = []
    with cr.inventory_request(time.time() + 0.02):
        context = cr.capture_finite_execution_context(TARGET, f)
    time.sleep(0.03)
    with ThreadPoolExecutor(1) as pool:
        assert pool.submit(invoke, context, f, lambda *_: calls.append(1)).result() == (None, None)
    assert calls == []


@pytest.mark.parametrize("raises", [False, True])
def test_worker_tls_restored_after_callback_and_exception(raises):
    f = frontier()
    with cr.inventory_request(time.time() + 1):
        context = cr.capture_finite_execution_context(TARGET, f)

    def callback(*_):
        assert cr.finite_request_remaining() is not None
        if raises:
            raise RuntimeError("owned callback failure")
        return {"status": "completed"}

    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(invoke, context, f, callback)
        if raises:
            with pytest.raises(RuntimeError):
                future.result()
        else:
            assert future.result()[0]["status"] == "completed"
        assert pool.submit(
            lambda: (cr.finite_request_remaining(), cr.finite_pool_current_binding())
        ).result() == (None, None)


def test_restore_cannot_lengthen_existing_worker_window():
    f = frontier()
    with cr.inventory_request(time.time() + 5):
        context = cr.capture_finite_execution_context(TARGET, f)
    with cr.inventory_request(time.time() + 1):
        old_window = cr._INVENTORY_REQUEST.window
        old_limit = cr._INVENTORY_REQUEST.monotonic_limit
        with cr.finite_execution_context(
            context, target=TARGET, frontier=f, deadline=time.time() + 4
        ):
            assert 0 < cr.finite_request_remaining() <= 1
            assert cr._INVENTORY_REQUEST.monotonic_limit <= old_limit
        assert cr._INVENTORY_REQUEST.window is old_window
        assert cr._INVENTORY_REQUEST.monotonic_limit == old_limit


def test_restore_refuses_another_existing_clock():
    f = frontier()
    with cr.inventory_request(time.time() + 5):
        context = cr.capture_finite_execution_context(TARGET, f)
    with cr.inventory_request(time.time() + 1, now=lambda: time.time()):
        with pytest.raises(cr.ResourceBlocked, match="another request clock"):
            with cr.finite_execution_context(
                context, target=TARGET, frontier=f, deadline=time.time() + 4
            ):
                pass


@pytest.mark.parametrize("mutation", ["target", "frontier", "binding", "process"])
def test_context_refuses_cross_binding(tmp_path, mutation):
    f = frontier()
    with slot(tmp_path / "pool", f), cr.inventory_request(time.time() + 1):
        context = cr.capture_finite_execution_context(TARGET, f)
        target = TARGET
        other = f
        if mutation == "target":
            target = "owner/other#2"
        elif mutation == "frontier":
            other = frontier("other-frontier")
        elif mutation == "binding":
            other = policy.make_frontier(
                [TARGET],
                owner=f["owner"],
                frontier_id=f["frontier_id"],
                bindings={TARGET: {"revision": "r2", "action_id": "a" * 64}},
            )
        else:
            context.pid = -1
        with pytest.raises(cr.ResourceBlocked):
            with cr.finite_execution_context(
                context, target=target, frontier=other, deadline=time.time() + 1
            ):
                pass


def test_capture_refuses_other_pool_frontier(tmp_path):
    f = frontier()
    with slot(tmp_path / "pool", f), cr.inventory_request(time.time() + 1):
        with pytest.raises(cr.ResourceBlocked, match="ownership"):
            cr.capture_finite_execution_context(TARGET, frontier("other"))


def test_closed_owner_cannot_be_borrowed(tmp_path):
    f = frontier()
    with slot(tmp_path / "pool", f), cr.inventory_request(time.time() + 1):
        context = cr.capture_finite_execution_context(TARGET, f)
    with pytest.raises(cr.ResourceBlocked, match="ownership"):
        invoke(context, f, lambda *_: pytest.fail("closed owner invoked"))


def test_one_held_slot_cannot_be_borrowed_concurrently(tmp_path):
    f = frontier()
    entered = threading.Event()
    release = threading.Event()
    with slot(tmp_path / "pool", f), cr.inventory_request(time.time() + 2):
        a = cr.capture_finite_execution_context(TARGET, f)
        b = cr.capture_finite_execution_context(TARGET, f)

        def first(*_):
            entered.set()
            assert release.wait(1)
            return {"status": "completed"}

        with ThreadPoolExecutor(2) as pool:
            one = pool.submit(invoke, a, f, first)
            try:
                assert entered.wait(1)
                two = pool.submit(invoke, b, f, lambda *_: pytest.fail("concurrent borrowed slot"))
                with pytest.raises(cr.ResourceBlocked, match="already borrowed"):
                    two.result()
            finally:
                release.set()
            assert one.result()[0]["status"] == "completed"


def test_legacy_callback_does_not_inherit_finite_context():
    seen = []
    with ThreadPoolExecutor(1) as pool:
        result = pool.submit(
            cq._invoke_before_cutoff,
            lambda *_: seen.append(cr.finite_request_remaining()) or {"status": "completed"},
            TARGET,
            "collect_evidence",
            {},
            {"deadline": time.time() + 1},
            time.time,
        ).result()
    assert result[0]["status"] == "completed" and seen == [None]
