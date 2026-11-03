"""Real queue/producer pre-POST exits; all external reads are disposable fixtures."""

import copy

import pytest
from test_closer_accepted_request_wait import TARGET, StaticRemote
from test_closer_queue import _run

import closer_dispatcher as dispatcher
import closer_queue as queue


def prepost_history(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setenv("ORCH_STORAGE_STATE_DIR", str(tmp_path / "storage"))
    remote = StaticRemote()
    ordinary_api = remote.api
    prepared = [False]

    def api(endpoint, **kwargs):
        if "/runs?" in endpoint:
            return {"total_count": 0, "workflow_runs": []}
        if endpoint == "repos/offline/repo":
            if mode == "metadata":
                raise TimeoutError("synthetic metadata read failure")
            if mode == "branch":
                return {}
            prepared[0] = True
        return ordinary_api(endpoint, **kwargs)

    remote.api = api
    original_snapshot = remote.snapshot

    def snapshot(target):
        if prepared[0] and mode == "freshness":
            raise TimeoutError("synthetic post-admission read failure")
        value = original_snapshot(target)
        if prepared[0] and mode == "binding":
            value["revision"] = "changed-after-prepare"
        return value

    remote.snapshot = snapshot
    backend = dispatcher.Closer(remote, state_path=tmp_path / "queue.json", apply=True)
    initial = backend.observe(TARGET)
    assert initial["action"] == "post_merge_verify" and initial["eligible"]

    class UncalledCore:
        def __getattr__(self, name):
            pytest.fail(f"pre-POST refusal entered Core: {name}")

    backend.runtime_binding = UncalledCore()
    backend.require_runtime_binding = True
    _run(
        backend.state_path,
        [TARGET],
        lambda _: copy.deepcopy(initial),
        backend.execute,
        max_passes=1,
    )
    op = queue._read_state(backend.state_path)["items"][TARGET]["operation"]
    assert op["status"] == "confirmed_not_started", op
    expected_reason = {
        "metadata": "verifier repository metadata unavailable: TimeoutError",
        "branch": "verifier workflow default branch unavailable",
        "freshness": "verification admission freshness unavailable: TimeoutError",
        "binding": "verification binding changed",
    }
    assert op["result"]["reason"] == expected_reason[mode]
    assert not remote.posts
    changed = {**initial, "revision": "later-revision", "action": "wait", "eligible": False}
    _run(
        backend.state_path,
        [TARGET],
        lambda _: copy.deepcopy(changed),
        backend.execute,
        max_passes=1,
    )
    history = queue._read_state(backend.state_path)["items"][TARGET]["operation_history"]
    assert history[-1]["status"] == "abandoned"
    return backend, initial, history[-1]


@pytest.mark.parametrize("mode", ["metadata", "branch", "freshness", "binding"])
def test_prepost_no_start_survives_abandoned_history(tmp_path, monkeypatch, mode):
    backend, initial, op = prepost_history(tmp_path, monkeypatch, mode)
    assert backend._unacknowledged_verification_request(initial) is None
    assert op["result"]["verification_no_start"]["phase"] == "before_core_verify"
    selected = backend.select(initial)
    assert selected[0] == "post_merge_verify" and selected[2] is True
    assert not any("core" in key for key in op["result"])


def cutoff_history(tmp_path, monkeypatch):
    from test_closer_verification_ack_barrier import world

    backend, initial = world(tmp_path, monkeypatch, "stale")
    clock = [1.0]
    executor = queue.ThreadPoolExecutor

    class DelayedExecutor(executor):
        def submit(self, fn, *args, **kwargs):
            clock[0] = 20.0
            return super().submit(fn, *args, **kwargs)

    monkeypatch.setattr(queue, "ThreadPoolExecutor", DelayedExecutor)

    def run(snapshot):
        return queue.run_round(
            backend.state_path,
            [initial["target"]],
            lambda _: copy.deepcopy(snapshot),
            lambda *_: pytest.fail("expired callback entered producer"),
            deadline=clock[0] + 19,
            now=lambda: clock[0],
            receipt_reserve_seconds=0,
            min_action_seconds=0,
            max_passes=1,
            claim_fn=lambda *_: True,
            release_fn=lambda *_: True,
            holder_fn=lambda *_: None,
        )

    run(initial)
    op = queue._read_state(backend.state_path)["items"][initial["target"]]["operation"]
    assert op["status"] == "confirmed_not_started" and "invoked_at" not in op
    monkeypatch.setattr(queue, "ThreadPoolExecutor", executor)
    changed = {**initial, "revision": "r2", "action": "wait", "eligible": False}
    run(changed)
    history = queue._read_state(backend.state_path)["items"][initial["target"]]["operation_history"]
    assert history[-1]["status"] == "abandoned"
    return backend, initial, history[-1]


def test_queue_cutoff_before_callback_survives_abandoned_history(tmp_path, monkeypatch):
    backend, initial, op = cutoff_history(tmp_path, monkeypatch)
    assert backend._unacknowledged_verification_request(initial) is None
    assert queue.verification_callback_not_invoked_proof(op, initial["target"]) is not None
    assert not any("core" in key for key in op["result"])


@pytest.mark.parametrize("kind", ["precore", "cutoff", "boolean"])
def test_callback_json_cannot_author_prepost_no_start(tmp_path, monkeypatch, kind):
    from test_closer_verification_ack_barrier import world

    backend, initial = world(tmp_path, monkeypatch, "stale")

    def forged(target, action, snapshot, operation):
        result = {"status": "not_started", "no_effect": True}
        if kind == "precore":
            result["verification_no_start"] = {
                "schema": "closer-verification-no-start/v1",
                "phase": "before_core_verify",
                "target": target,
                "operation_id": operation["id"],
                "action": action,
                "revision": operation["revision"],
                "binding_sha256": dispatcher.digest(operation["binding"]),
                "result_status": "not_started",
                "post_started": False,
            }
        elif kind == "cutoff":
            result["verification_callback_not_invoked"] = {
                "schema": "closer-verification-callback-not-invoked/v1",
                "target": target,
                "operation_id": operation["id"],
                "action": action,
                "revision": operation["revision"],
                "binding_sha256": dispatcher.digest(operation["binding"]),
                "deadline": operation["deadline"],
                "observed_at": operation["deadline"],
                "callback_invoked": False,
            }
        return result

    _run(
        backend.state_path,
        [initial["target"]],
        lambda _: copy.deepcopy(initial),
        forged,
        max_passes=1,
    )
    changed = {**initial, "revision": "r2", "action": "wait", "eligible": False}
    _run(
        backend.state_path,
        [initial["target"]],
        lambda _: copy.deepcopy(changed),
        forged,
        max_passes=1,
    )
    assert backend._unacknowledged_verification_request(initial) is not None


@pytest.mark.parametrize("kind", ["precore", "cutoff"])
@pytest.mark.parametrize(
    "damage",
    [
        "id",
        "revision",
        "binding",
        "target",
        "extra",
        "missing",
        "unknown",
        "post_started",
        "accepted",
        "running",
        "invoked",
    ],
)
def test_original_no_start_identity_and_effect_conflicts_remain_fenced(
    tmp_path, monkeypatch, kind, damage
):
    import json

    if kind == "precore":
        backend, initial, op = prepost_history(tmp_path, monkeypatch, "metadata")
        key = "verification_no_start"
    else:
        backend, initial, op = cutoff_history(tmp_path, monkeypatch)
        key = "verification_callback_not_invoked"
    if damage in {"id", "revision"}:
        op[damage] = "foreign"
    elif damage == "binding":
        op["binding"]["extra"] = "foreign"
    elif damage == "target":
        op["result"][key]["target"] = "foreign/repo#9"
    elif damage == "extra":
        op["result"][key]["self_authorized"] = True
    elif damage == "missing":
        op["result"].pop(key)
    elif damage == "unknown":
        op["result"]["outcome_uncertain"] = True
    elif damage == "post_started":
        op["result"]["post_started"] = True
    elif damage == "accepted":
        op["result"]["verification_post_accepted"] = {"request_basis": "a" * 64}
    elif damage == "running":
        op["status"] = "running"
    elif kind == "cutoff":
        op["invoked_at"] = op["deadline"] - 1
    else:
        op.pop("invoked_at")
    state = queue._read_state(backend.state_path)
    state["items"][initial["target"]]["operation_history"][-1] = op
    backend.state_path.write_text(json.dumps(state))
    assert backend._unacknowledged_verification_request(initial) is not None


def test_precore_no_start_cannot_be_stamped_outside_live_queue(tmp_path, monkeypatch):
    backend, initial, op = prepost_history(tmp_path, monkeypatch, "metadata")
    backend.state_path.unlink()
    op["status"] = "submitted"
    result = backend.execute(initial["target"], op["action"], initial, op)
    assert result["status"] == "not_started"
    assert "verification_no_start" not in result
    assert "verification_rejection_proof" not in result
    assert "core_rejected_not_accepted" not in result


def test_arbitrary_preparation_not_started_json_is_not_stamped(tmp_path, monkeypatch):
    backend, initial, _op = prepost_history(tmp_path, monkeypatch, "metadata")
    backend.state_path.unlink()
    monkeypatch.setattr(
        backend, "prepare_verify", lambda *_: {"status": "not_started", "no_effect": True}
    )
    _run(
        backend.state_path,
        [initial["target"]],
        lambda _: copy.deepcopy(initial),
        backend.execute,
        max_passes=1,
    )
    op = queue._read_state(backend.state_path)["items"][initial["target"]]["operation"]
    assert "verification_no_start" not in op["result"]


def test_unknown_callback_result_cannot_retain_precore_no_start(tmp_path, monkeypatch):
    backend, initial, _op = prepost_history(tmp_path, monkeypatch, "metadata")
    backend.state_path.unlink()

    def unknown(*args):
        result = backend.execute(*args)
        assert "verification_no_start" in result
        return {**result, "status": "uncertain", "outcome_uncertain": True}

    _run(
        backend.state_path,
        [initial["target"]],
        lambda _: copy.deepcopy(initial),
        unknown,
        max_passes=1,
    )
    op = queue._read_state(backend.state_path)["items"][initial["target"]]["operation"]
    assert op["status"] == "uncertain"
    assert "verification_no_start" not in op["result"]
    assert backend._unacknowledged_verification_request(initial) is not None


def test_cancelled_future_without_cutoff_receipt_stays_owned(tmp_path, monkeypatch):
    from concurrent.futures import Future

    from test_closer_verification_ack_barrier import world

    backend, initial = world(tmp_path, monkeypatch, "stale")
    executor = queue.ThreadPoolExecutor

    class CancelledExecutor(executor):
        def submit(self, *_args, **_kwargs):
            future = Future()
            assert future.cancel()
            return future

    monkeypatch.setattr(queue, "ThreadPoolExecutor", CancelledExecutor)
    _run(
        backend.state_path,
        [initial["target"]],
        lambda _: copy.deepcopy(initial),
        lambda *_: pytest.fail("cancelled callback entered"),
        max_passes=1,
    )
    op = queue._read_state(backend.state_path)["items"][initial["target"]]["operation"]
    assert op["status"] == "uncertain"
    assert "result" not in op
    assert backend._unacknowledged_verification_request(initial) is not None
