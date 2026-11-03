"""Static remote snapshots: durable POST acknowledgments, no invented progress."""

import copy

import pytest

import closer_dispatcher as dispatcher
import closer_queue as queue
from closer_frontier_policy import action_identity, make_frontier

TARGET = "offline/repo#42"


class StaticRemote:
    def __init__(self):
        self.current = {
            "target": TARGET,
            "kind": "pr",
            "state": "MERGED",
            "head": "a" * 40,
            "base": "c" * 40,
            "merge": "b" * 40,
            "revision": "static-source",
            "review_revision": "reviews-1",
            "sources": [
                {
                    "target": "offline/repo#2",
                    "body": "## Tasks\n- [ ] Preserve behavior",
                    "state": "open",
                    "labels": [],
                }
            ],
            "checks": "SUCCESS",
            "active_threads": 0,
            "threads": [],
            "labels": [],
            "verifier": None,
            "verifier_history_complete": True,
        }
        self.posts = []
        self.reject = False

    def snapshot(self, target):
        assert target == TARGET
        return copy.deepcopy(self.current)

    def api(self, endpoint, *, method="GET", fields=None, **_kwargs):
        if method == "GET":
            assert endpoint == "repos/offline/repo"
            return {"default_branch": "main"}
        assert method == "POST"
        assert endpoint == "repos/offline/repo/actions/workflows/agents-verifier.yml/dispatches"
        assert fields["ref"] == "main" and fields["inputs[mode]"] == "compare"
        assert fields["inputs[pr_number]"] == "42"
        self.posts.append(copy.deepcopy(fields))
        if self.reject:
            raise dispatcher.GitHubNoEffectRejected("HTTP 422: exact inputs rejected")
        # HTTP 204 accepted; do not invent a label, marker, run or source revision.
        return None


def forbidden(*_args, **_kwargs):
    pytest.fail("existing request must not start another POST or model")


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    remote = StaticRemote()
    path = tmp_path / "queue.json"
    backend = dispatcher.Closer(
        remote, state_path=path, workers=True, apply=True, offload_fn=forbidden
    )
    initial = backend.observe(TARGET)
    assert initial["action"] == "post_merge_verify" and initial["eligible"]
    frontier = make_frontier(
        [TARGET],
        owner="original-owner",
        frontier_id="static-accepted",
        bindings={
            TARGET: {
                "revision": initial["revision"],
                "action_id": action_identity(
                    TARGET, initial["revision"], initial["action"], initial["operation_binding"]
                ),
            }
        },
    )
    backend.finite_frontier = frontier
    return backend, remote, path, frontier


def run(backend, path, frontier, execute, observe=None):
    return queue.run_round(
        path,
        [TARGET],
        observe or backend.observe,
        execute,
        owner="queue-owner",
        deadline=None,
        receipt_reserve_seconds=0,
        max_operation_retries=0,
        finite_frontier=frontier,
        frontier_target=TARGET,
        request_timeout_seconds=10,
        max_passes=1,
        claim_fn=lambda *_: True,
        release_fn=lambda *_: True,
        holder_fn=lambda *_: None,
    )


def submit(backend, remote):
    current = remote.snapshot(TARGET)
    basis = backend.verification_request_basis(current)
    return backend.submit_verify(
        TARGET,
        current,
        {
            "mutation_kind": "workflow_dispatch",
            "endpoint": "repos/offline/repo/actions/workflows/agents-verifier.yml/dispatches",
            "request_basis": basis,
            "fields": {
                "ref": "main",
                "inputs[pr_number]": "42",
                "inputs[mode]": "compare",
                "inputs[verification_request_basis]": basis,
            },
        },
    )


def proof(path, frontier, receipt, backend, remote):
    row = receipt["accounting"][0]
    return queue.accepted_verifier_wait_proof(
        path,
        TARGET,
        frontier=frontier,
        receipt=receipt,
        evidence={
            "target": TARGET,
            "event_id": backend.verification_request_basis(remote.current),
            "operation_id": row["operation_id"],
            "head": remote.current["head"],
            "merge": remote.current["merge"],
            "observed_revision": row["revision"],
        },
    )


def test_static_post_ack_is_durable_before_first_followup_observation(world):
    backend, remote, path, frontier = world
    original = copy.deepcopy(remote.current)
    disk_seen = []

    def observe(target):
        if remote.posts:
            op = queue._read_state(path)["frontiers"][frontier["frontier_id"]]["items"][target][
                "operation"
            ]
            disk_seen.append(op)
            assert op["status"] == "completed"
            assert (
                op["result"]["verification_post_accepted"]["request_basis"]
                == remote.posts[0]["inputs[verification_request_basis]"]
            )
        return backend.observe(target)

    receipt = run(backend, path, frontier, lambda *_: submit(backend, remote), observe)
    assert disk_seen and len(remote.posts) == 1
    assert remote.current == original
    row = receipt["accounting"][0]
    assert row["action"] == "collect_evidence" and row["eligible"] is False
    assert row["reason"] == "collect terminal or unattributed verifier evidence; do not rerun"
    assert row["revision"] == original["revision"]
    assert receipt["stop_valid"] is True
    accepted = proof(path, frontier, receipt, backend, remote)
    assert accepted and accepted["schema"] == "closer-accepted-verifier-wait/v1"
    again = run(backend, path, frontier, forbidden)
    assert again["stop_valid"] and len(remote.posts) == 1
    assert proof(path, frontier, again, backend, remote)


@pytest.mark.parametrize(
    "damage", ["missing", "basis", "kind", "malformed_kind", "uncertain", "no_effect"]
)
def test_unacknowledged_old_request_never_becomes_a_fresh_post(world, monkeypatch, damage):
    backend, remote, path, frontier = world

    def execute(*_args):
        result = submit(backend, remote)
        if damage == "missing":
            result.pop("verification_post_accepted")
        elif damage == "basis":
            result["verification_post_accepted"]["request_basis"] = "f" * 64
        elif damage == "kind":
            result["verification_post_accepted"]["mutation_kind"] = "invented"
        elif damage == "malformed_kind":
            result["verification_post_accepted"]["mutation_kind"] = []
        else:
            result["outcome_uncertain" if damage == "uncertain" else "no_effect"] = True
        return result

    receipt = run(backend, path, frontier, execute)
    assert len(remote.posts) == 1
    assert backend._accepted_verification_request(remote.current) is None
    assert proof(path, frontier, receipt, backend, remote) is None
    old_op = copy.deepcopy(
        queue._read_state(path)["frontiers"][frontier["frontier_id"]]["items"][TARGET]["operation"]
    )
    assert backend.select(remote.current)[:3] == (
        "collect_evidence",
        "verification request acknowledgement unresolved; reconcile original operation",
        False,
    )
    monkeypatch.setattr(backend, "_verification_preflight", forbidden)
    assert backend.prepare_verify(TARGET, remote.current)["status"] == "not_started"
    run(backend, path, frontier, forbidden)
    assert len(remote.posts) == 1
    # A refreshed source/frontier must not erase the exact old ownership barrier.
    remote.current["revision"] = "new-source-revision"
    remote.current["sources"][0]["body"] += "\n- [ ] Additional source requirement"
    observation = backend.observe(TARGET)
    assert observation["eligible"] is False and observation["action"] == "collect_evidence"
    fresh = make_frontier(
        [TARGET],
        owner="original-owner",
        frontier_id="refreshed",
        bindings={
            TARGET: {
                "revision": observation["revision"],
                "action_id": action_identity(
                    TARGET,
                    observation["revision"],
                    observation["action"],
                    observation["operation_binding"],
                ),
            }
        },
    )
    backend.finite_frontier = fresh
    run(backend, path, fresh, forbidden)
    assert backend.prepare_verify(TARGET, remote.current)["status"] == "not_started"
    assert len(remote.posts) == 1
    assert (
        queue._read_state(path)["frontiers"][frontier["frontier_id"]]["items"][TARGET]["operation"]
        == old_op
    )


def test_explicit_known_not_started_is_distinct_from_missing_ack(world, monkeypatch):
    backend, remote, path, frontier = world
    remote.reject = True
    receipt = run(backend, path, frontier, lambda *_: submit(backend, remote))
    op = queue._read_state(path)["frontiers"][frontier["frontier_id"]]["items"][TARGET]["operation"]
    assert op["status"] == "confirmed_not_started"
    assert op["result"]["verification_rejected"]["http_status"] == 422
    assert backend._unacknowledged_verification_request(remote.current) is None
    assert backend._accepted_verification_request(remote.current) is None
    assert backend.select(remote.current)[0] == "post_merge_verify"
    monkeypatch.setattr(backend, "_verification_preflight", lambda *_a, **_k: None)
    prepared = backend.prepare_verify(TARGET, remote.current)
    assert prepared["mutation_kind"] == "workflow_dispatch"
    assert len(remote.posts) == 1  # Preparing the legitimate retry has no effect.
    assert proof(path, frontier, receipt, backend, remote) is None


def test_unknown_request_can_reach_only_its_original_reconciler(world):
    """The injected binding models an already-authenticated terminal readback.

    It settles only the old effect. No new acknowledgment/retry is manufactured;
    reconstructing a legacy missing-ack result remains a separate interface gap.
    """
    backend, remote, path, frontier = world

    def uncertain(*_args):
        result = submit(backend, remote)
        result.update(status="uncertain", outcome_uncertain=True)
        result.pop("verification_post_accepted")
        return result

    run(backend, path, frontier, uncertain)
    old = copy.deepcopy(
        queue._read_state(path)["frontiers"][frontier["frontier_id"]]["items"][TARGET]["operation"]
    )
    basis = old["result"]["verification_request_basis"]
    terminal = {
        "operation_id": old["id"],
        "revision": old["revision"],
        "target": TARGET,
        "head": remote.current["head"],
        "merge": remote.current["merge"],
        "request_basis": basis,
    }
    calls = []

    class OriginalBinding:
        def reconcile(self, target, bounded):
            calls.append((target, copy.deepcopy(bounded)))
            assert target == TARGET and bounded["id"] == terminal["operation_id"]
            assert bounded["revision"] == terminal["revision"]
            assert bounded["binding"] == old["binding"]
            assert bounded["effect_deadline"] == old["deadline"]
            # Existing native effect reconciliation supplies completion evidence,
            # not permission to invent the original POST acknowledgment.
            return {
                "status": "completed",
                "result": {"verification_request_basis": basis, "reconciled_terminal": terminal},
            }

    backend.runtime_binding = OriginalBinding()
    before = path.read_bytes()
    assert backend._unacknowledged_verification_request(remote.current)["operation_id"] == old["id"]
    assert backend.prepare_verify(TARGET, remote.current)["status"] == "not_started"
    assert calls == [] and path.read_bytes() == before
    result = queue.reconcile_only(
        path,
        TARGET,
        old["id"],
        old["revision"],
        frontier_id=frontier["frontier_id"],
        reconcile=backend.reconcile,
    )
    assert result["status"] == "completed" and len(calls) == 1
    actual = queue._read_state(path)["frontiers"][frontier["frontier_id"]]["items"][TARGET][
        "operation"
    ]
    assert actual["id"] == old["id"] and actual["revision"] == old["revision"]
    assert actual["reconciliation"]["result"]["reconciled_terminal"] == terminal
    assert backend._accepted_verification_request(remote.current) is None
    assert backend._unacknowledged_verification_request(remote.current) is not None
    assert backend.prepare_verify(TARGET, remote.current)["status"] == "not_started"
    assert len(remote.posts) == 1


def test_legacy_completed_missing_ack_has_no_implicit_terminal_repair_interface(world):
    """Document the existing completed-record reconciler limit honestly.

    Observing a terminal result or crossing a deadline cannot repair acknowledgment
    metadata. The existing reconciler treats completed records as idempotent;
    explicit legacy result repair needs a separately reviewed exact-proof bridge.
    """
    backend, remote, path, frontier = world

    def missing_ack(*_args):
        result = submit(backend, remote)
        result.pop("verification_post_accepted")
        return result

    run(backend, path, frontier, missing_ack)
    old = queue._read_state(path)["frontiers"][frontier["frontier_id"]]["items"][TARGET][
        "operation"
    ]
    before = path.read_bytes()
    result = queue.reconcile_only(
        path,
        TARGET,
        old["id"],
        old["revision"],
        frontier_id=frontier["frontier_id"],
        reconcile=forbidden,
    )
    assert result == {"status": "completed", "operation_id": old["id"], "idempotent": True}
    assert path.read_bytes() == before
    assert backend._accepted_verification_request(remote.current) is None
    assert backend.prepare_verify(TARGET, remote.current)["status"] == "not_started"
    assert backend._unacknowledged_verification_request(remote.current)["operation_id"] == old["id"]
    assert len(remote.posts) == 1


@pytest.mark.parametrize(
    "status", ["submitted", "running", "uncertain", "unknown", "abandoned", None]
)
def test_pending_old_request_without_basis_remains_owned_across_new_frontier(
    world, monkeypatch, status
):
    backend, remote, path, frontier = world
    run(backend, path, frontier, lambda *_: submit(backend, remote))
    document = queue._read_state(path)
    original = document["frontiers"][frontier["frontier_id"]]["items"][TARGET]["operation"]
    original["status"] = status
    original["result"].pop("verification_request_basis")
    original["result"].pop("verification_post_accepted")
    queue._atomic_write(path, document)
    before = copy.deepcopy(original)
    # Incomplete result metadata does not prove that the old POST never started.
    selected = backend.select(remote.current)
    assert selected[0] == "collect_evidence" and selected[2] is False
    assert "reconcile original operation" in selected[1]
    monkeypatch.setattr(backend, "_verification_preflight", forbidden)
    assert backend.prepare_verify(TARGET, remote.current)["status"] == "not_started"
    remote.current["revision"] = "refreshed-without-outcome-metadata"
    # A fresh controller plans outside the old namespace; malformed historical
    # state must remain a barrier rather than becoming a new request.
    del backend.finite_frontier
    observation = backend.observe(TARGET)
    fresh = make_frontier(
        [TARGET],
        owner="original-owner",
        frontier_id="unresolved-missing-basis-" + str(status),
        bindings={
            TARGET: {
                "revision": observation["revision"],
                "action_id": action_identity(
                    TARGET,
                    observation["revision"],
                    observation["action"],
                    observation["operation_binding"],
                ),
            }
        },
    )
    backend.finite_frontier = fresh
    assert backend._accepted_verification_request(remote.current) is None
    run(backend, path, fresh, forbidden)
    assert len(remote.posts) == 1
    assert (
        queue._read_state(path)["frontiers"][frontier["frontier_id"]]["items"][TARGET]["operation"]
        == before
    )


def test_first_actual_execute_keeps_only_its_live_queue_submission(world, monkeypatch):
    backend, remote, path, frontier = world
    # Exercise real execute->select->prepare->fresh select->submit; only the
    # remote inventory seam is pre-admitted, with no tools/providers involved.
    monkeypatch.setattr(backend, "_verification_preflight", lambda *_a, **_k: None)
    receipt = run(backend, path, frontier, backend.execute)
    assert len(remote.posts) == 1
    assert receipt["stop_valid"] is True
    assert proof(path, frontier, receipt, backend, remote)
    run(backend, path, frontier, forbidden)
    assert len(remote.posts) == 1


def callback_operation():
    return {
        "id": "live-original",
        "owner": "queue-owner",
        "action": "post_merge_verify",
        "status": "submitted",
        "revision": "static-source",
        "deadline": 20.0,
        "binding": {"pr_target": TARGET, "head": "a" * 40, "merge": "b" * 40},
    }


def test_callback_exemption_is_exact_process_thread_operation_and_deadline(monkeypatch):
    import threading

    operation = callback_operation()
    clock = [10.0]
    other_threads = []

    def execute(target, action, snapshot, current):
        assert queue._currently_invoking(TARGET, operation)
        for field in ("id", "owner", "revision", "binding"):
            different = copy.deepcopy(operation)
            different[field] = {"head": "f" * 40} if field == "binding" else "different"
            assert not queue._currently_invoking(TARGET, different)
        assert not queue._currently_invoking("offline/repo#99", operation)
        thread = threading.Thread(
            target=lambda: other_threads.append(queue._currently_invoking(TARGET, operation))
        )
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive() and other_threads == [False]
        actual_pid = queue.os.getpid()
        with monkeypatch.context() as scoped:
            scoped.setattr(queue.os, "getpid", lambda: actual_pid + 1)
            assert not queue._currently_invoking(TARGET, operation)
        clock[0] = operation["deadline"]
        assert not queue._currently_invoking(TARGET, operation)
        return {"status": "completed"}

    assert not queue._currently_invoking(TARGET, operation)
    result, invoked = queue._invoke_before_cutoff(
        execute, TARGET, "post_merge_verify", {}, operation, lambda: clock[0]
    )
    assert result["status"] == "completed" and invoked == 10.0
    assert not queue._currently_invoking(TARGET, operation)
    assert not hasattr(queue._callback_threads, "invocation")
    assert set(operation) == {"id", "owner", "action", "status", "revision", "deadline", "binding"}


def test_nested_callback_refuses_and_finally_clears_even_after_exception():
    operation = callback_operation()
    calls = []

    def outer(*_args):
        assert queue._currently_invoking(TARGET, operation)
        with pytest.raises(queue.QueueStateError, match="nested queue callback"):
            queue._invoke_before_cutoff(
                lambda *_a: calls.append("nested"),
                TARGET,
                "post_merge_verify",
                {},
                operation,
                lambda: 10.0,
            )
        assert queue._currently_invoking(TARGET, operation)
        raise RuntimeError("callback interrupted before its outcome")

    with pytest.raises(RuntimeError, match="callback interrupted"):
        queue._invoke_before_cutoff(outer, TARGET, "post_merge_verify", {}, operation, lambda: 10.0)
    assert calls == [] and not queue._currently_invoking(TARGET, operation)
    assert not hasattr(queue._callback_threads, "invocation")


def test_expired_callback_never_receives_an_invocation_exemption():
    operation = callback_operation()
    result, invoked = queue._invoke_before_cutoff(
        forbidden, TARGET, "post_merge_verify", {}, operation, lambda: 20.0
    )
    assert result is None and invoked is None
    assert not queue._currently_invoking(TARGET, operation)


def test_same_thread_other_callback_cannot_adopt_an_old_missing_basis_operation(world):
    backend, remote, path, frontier = world
    run(backend, path, frontier, lambda *_: submit(backend, remote))
    document = queue._read_state(path)
    old = document["frontiers"][frontier["frontier_id"]]["items"][TARGET]["operation"]
    old.update(status="submitted", result={})
    queue._atomic_write(path, document)
    different = copy.deepcopy(old)
    different["id"] = "other-live-operation"
    different.pop(
        "finite_frontier"
    )  # A separate bounded legacy callback, not a borrowed finite lease.

    def callback(*_args):
        assert queue._currently_invoking(TARGET, different)
        assert not queue._currently_invoking(TARGET, old)
        assert backend.select(remote.current)[2] is False
        assert backend.prepare_verify(TARGET, remote.current)["status"] == "not_started"
        return {"status": "blocked"}

    result, _invoked = queue._invoke_before_cutoff(
        callback,
        TARGET,
        "post_merge_verify",
        {},
        different,
        lambda: different["request_started_at"],
    )
    assert result["status"] == "blocked" and len(remote.posts) == 1


@pytest.mark.parametrize("valid", [True, False])
def test_resource_wait_exemption_requires_independent_exact_no_start_proof(
    world, monkeypatch, valid
):
    backend, remote, path, frontier = world
    # No POST has been called. Build the supported historical native prewrite
    # contract and validate it through the real proof checker, not its status.
    waiting = {
        "schema": "closer-verifier-prewrite-wait/v1",
        "boundary": "before_verify_label_post",
        "target": TARGET,
        "subject_pr": TARGET,
        "revision": remote.current["revision"],
        "head": remote.current["head"],
        "merge": remote.current["merge"],
        "observed_at": 11.0,
        "endpoint": "repos/offline/repo/actions/workflows/agents-verifier.yml/runs?per_page=100&status=in_progress",
        "blocking_run": {
            "id": 99,
            "run_attempt": 1,
            "path": ".github/workflows/agents-verifier.yml",
            "event": "workflow_dispatch",
            "status": "in_progress",
            "conclusion": None,
            "repository": {"full_name": "offline/repo"},
            "head_sha": "e" * 40,
            "pull_requests": [{"number": 42}],
        },
    }
    waiting["sha256"] = queue._digest(waiting)
    operation = {
        "id": "original-resource-wait",
        "owner": "original-owner",
        "action": "post_merge_verify",
        "status": "resource_wait",
        "revision": remote.current["revision"],
        "binding": backend.operation_binding(TARGET, remote.current),
        "finite_frontier": frontier,
        "invoked_at": 10.0,
        "completed_at": 12.0,
        "result": {"verifier_resource_wait": waiting},
    }
    assert queue.verifier_resource_wait_proof(operation, TARGET) is not None
    if not valid:
        waiting["head"] = "f" * 40
        waiting["sha256"] = queue._digest(
            {key: value for key, value in waiting.items() if key != "sha256"}
        )
        assert queue.verifier_resource_wait_proof(operation, TARGET) is None
    document = queue._new_state()
    document["frontiers"] = {
        frontier["frontier_id"]: {
            "policy": frontier,
            "items": {TARGET: {"operation": operation}},
            "round": {},
        }
    }
    queue._atomic_write(path, document)
    before = path.read_bytes()
    if valid:
        assert backend._unacknowledged_verification_request(remote.current) is None
        assert backend.select(remote.current)[0] == "post_merge_verify"
        monkeypatch.setattr(backend, "_verification_preflight", lambda *_a, **_k: None)
        prepared = backend.prepare_verify(TARGET, remote.current)
        assert prepared["mutation_kind"] == "workflow_dispatch"
    else:
        assert (
            backend._unacknowledged_verification_request(remote.current)["operation_id"]
            == operation["id"]
        )
        assert backend.select(remote.current)[2] is False
        monkeypatch.setattr(backend, "_verification_preflight", forbidden)
        assert backend.prepare_verify(TARGET, remote.current)["status"] == "not_started"
    assert path.read_bytes() == before and remote.posts == []
