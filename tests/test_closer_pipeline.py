from __future__ import annotations

import hashlib
import json
import threading
import time

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

    run_pipeline(path, targets, **cb, capacity_fn=cap, deadline=time.time() + 10)
    assert len(interpretations) == 40
    changed["target"] = "t17"
    run_pipeline(path, targets, **cb, capacity_fn=cap, deadline=time.time() + 10)
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
            assert action_started.wait(1)
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
    receipts = []
    worker = threading.Thread(
        target=lambda: receipts.append(
            run_pipeline(
                tmp_path / "state.json",
                targets,
                **cb,
                capacity_fn=lambda: {"io": 4, "low": 2, "exception": 1},
                deadline=time.time() + 3,
                max_io_workers=4,
            )
        )
    )
    worker.start()
    assert slow_prepares.wait(0.5)
    assert action_started.wait(0.75), "ready action failed to use the fourth IO slot"
    worker.join(3)
    assert not worker.is_alive()
    assert all(row["status"] == "completed" for row in receipts[0]["targets"].values())


def test_completion_drives_next_stage_without_waiting_for_slow_peer(tmp_path):
    gate = threading.Event()
    fast_act = threading.Event()

    def prep(target, ctx):
        if target == "slow":
            gate.wait(2)
        return {"revision": target}

    cb = callbacks(
        act=lambda t, c: (fast_act.set() if t == "fast" else None)
        or {"status": "completed", "target_complete": True}
    )
    cb["prepare"] = prep
    receipts = []
    worker = threading.Thread(
        target=lambda: receipts.append(
            run(
                tmp_path / "state.json",
                ["slow", "fast"],
                cb,
                seconds=1,
                max_io_workers=2,
                max_low_workers=2,
            )
        )
    )
    worker.start()
    assert fast_act.wait(0.5), "fast target stalled behind slow target"
    gate.set()
    worker.join()
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

    def capacity():
        capacity_calls.append(1)
        amount = 1 if len(capacity_calls) == 1 else 0
        return {"io": amount, "low": 2, "exception": 1}

    cb = callbacks()
    cb["prepare"] = lambda t, c: starts.append(t) or {"revision": t}
    receipt = run_pipeline(
        tmp_path / "state.json", ["a", "b"], **cb, capacity_fn=capacity, deadline=time.time() + 0.08
    )
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
    entered, release = threading.Event(), threading.Event()
    starts = []
    cb = callbacks()

    def prep(target, ctx):
        starts.append((target, time.time()))
        if target == "a":
            entered.set()
            release.wait(2)
        return {"revision": target}

    cb["prepare"] = prep
    first_receipts = []
    deadline = time.time() + 0.12
    first = threading.Thread(
        target=lambda: first_receipts.append(
            run_pipeline(
                path,
                ["a", "b"],
                **cb,
                capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
                deadline=deadline,
            )
        )
    )
    first.start()
    assert entered.wait(0.5)
    time.sleep(0.15)  # pass the first deadline while its callback is still owned
    second_receipts = []
    second = threading.Thread(
        target=lambda: second_receipts.append(
            run_pipeline(
                path,
                ["a", "b"],
                **callbacks(),
                capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
                deadline=time.time() + 1,
            )
        )
    )
    second.start()
    time.sleep(0.05)
    assert second.is_alive(), "a new round bypassed the old callback's run lock"
    release.set()
    first.join(1)
    second.join(1)
    assert not first.is_alive() and not second.is_alive()
    assert first_receipts[0]["elapsed_seconds"] > 0.12
    # The first round cannot submit the queued target's callback after cutoff.
    assert all(started_at < deadline for _, started_at in starts)
    assert [target for target, _ in starts] == ["a"]


def test_typed_prepare_exhaustion_waits_for_cutoff_and_defers_unstarted_targets(tmp_path):
    path = tmp_path / "pipeline.json"
    starts = []
    deadline = time.time() + 0.25
    cb = callbacks()

    def prepare(target, context):
        starts.append(target)
        time.sleep(max(0.0, deadline + 0.02 - time.time()))
        remaining = deadline - time.time()
        raise FakeRoundDeadlineExhausted(deadline, remaining)

    cb["prepare"] = prepare
    began = time.time()
    receipt = run_pipeline(
        path,
        ["a", "b", "c"],
        **cb,
        capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
        deadline=deadline,
        max_io_workers=1,
        round_deadline_exception=FakeRoundDeadlineExhausted,
    )
    assert time.time() >= deadline
    assert receipt["elapsed_seconds"] >= deadline - began
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
