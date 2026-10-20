"""Finite work duration does not weaken target ownership or request bounds."""

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path

import pytest

import closer_pipeline as pipeline
import closer_quarantine as quarantine
import closer_resources as resources
import dispatcher
from closer_dispatcher import bounded_run
from closer_frontier_policy import action_identity, make_frontier


def frontier(targets=("owner/repo#1",)):
    return make_frontier(
        list(targets),
        owner="campaign-owner",
        frontier_id="frozen-1",
        bindings={
            target: {
                "revision": "head-a",
                "action_id": action_identity(target, "head-a", "merge", {}),
            }
            for target in targets
        },
    )


def test_finite_lease_uses_held_owner_epoch_not_elapsed_work(tmp_path):
    root = resources.open_root(tmp_path / "state.json", pipeline_schema=pipeline.SCHEMA)
    selected = frontier()
    lease = resources.acquire(
        root, "owner/repo#1", owner="first", finite_frontier=selected, now=lambda: 10
    )
    try:
        lease.check(lambda: 1_000_000)
        assert (
            resources.acquire(
                root,
                "owner/repo#1",
                owner="second",
                finite_frontier=selected,
                now=lambda: 1_000_000,
            )
            is None
        )
        record = lease.directory / "lease.json"
        doc = json.loads(record.read_text())
        assert "expires_at" not in doc
        assert doc["finite_frontier"] == selected
        doc["owner"] = "second"
        record.write_text(json.dumps(doc))
        with pytest.raises(resources.StaleLease, match="superseded"):
            lease.check(lambda: 1_000_000)
    finally:
        lease.release()
    with pytest.raises(resources.StaleLease, match="released"):
        lease.check(lambda: 10)


@pytest.mark.parametrize("mutation", ["descriptor", "epoch_type"])
def test_finite_lease_rejects_descriptor_drift_and_duration(tmp_path, mutation):
    root = resources.open_root(tmp_path / "state.json", pipeline_schema=pipeline.SCHEMA)
    with pytest.raises(ValueError, match="no duration"):
        resources.acquire(
            root, "owner/repo#1", owner="one", expires_at=20, finite_frontier=frontier()
        )
    with pytest.raises(ValueError, match="selected frontier"):
        resources.acquire(root, "owner/repo#2", owner="one", finite_frontier=frontier())
    lease = resources.acquire(root, "owner/repo#1", owner="one", finite_frontier=frontier())
    try:
        record = lease.directory / "lease.json"
        doc = json.loads(record.read_text())
        if mutation == "descriptor":
            doc["finite_frontier"]["frontier_id"] = "replacement"
        else:
            doc["epoch"] = float(doc["epoch"])
        record.write_text(json.dumps(doc))
        with pytest.raises(resources.StaleLease, match="identity changed"):
            lease.check()
    finally:
        lease.release()


def test_legacy_lease_keeps_original_expiry(tmp_path):
    root = resources.open_root(tmp_path / "state.json", pipeline_schema=pipeline.SCHEMA)
    lease = resources.acquire(root, "owner/repo#1", owner="legacy", expires_at=20, now=lambda: 10)
    try:
        with pytest.raises(resources.StaleLease, match="expired"):
            lease.check(lambda: 21)
    finally:
        lease.release()


def test_finite_request_remaining_uses_original_wall_and_monotonic_window(monkeypatch):
    wall, monotonic = [100.0], [10.0]
    monkeypatch.setattr(time, "monotonic", lambda: monotonic[0])
    assert resources.finite_request_remaining() is None
    with resources.inventory_request(105.0, now=lambda: wall[0]):
        assert resources.finite_request_remaining() == 5.0
        wall[0], monotonic[0] = 90.0, 12.0
        assert resources.finite_request_remaining() == 3.0
        with resources.inventory_request(200.0, now=lambda: wall[0]):
            assert resources.finite_request_remaining() == 3.0
        wall[0] = 106.0
        assert resources.finite_request_remaining() == 0.0
        wall[0], monotonic[0] = 90.0, 15.0
        assert resources.finite_request_remaining() == 0.0
    assert resources.finite_request_remaining() is None


@pytest.mark.parametrize(
    "deadline,clock", [(True, 100), (float("inf"), 100), (105, float("nan")), (105, False)]
)
def test_finite_request_remaining_rejects_invalid_clocks_without_context_leak(deadline, clock):
    with pytest.raises(resources.ResourceBlocked, match="finite numeric"):
        with resources.inventory_request(deadline, now=lambda: clock):
            pytest.fail("invalid request clock accepted")
    assert resources.finite_request_remaining() is None


def run_selected(path, selected, **overrides):
    request_timeout = overrides.pop("request_timeout_seconds", 100)
    callbacks = dict(
        prepare=lambda t, c: {"revision": "head-a"},
        interpret=lambda t, c: {"substantive_needs": []},
        act=lambda t, c: {"status": "completed", "target_complete": True},
        exception=lambda t, c: {},
        capacity_fn=lambda: {"io": 4, "low": 2, "exception": 1},
        resource_keys=lambda *_: [],
    )
    callbacks.update(overrides)
    # These tests exercise the private scheduler/resource mechanics with local
    # fakes. Public finite entry points separately reject callbacks that do not
    # come from the registered production dispatcher.
    original = pipeline._require_dispatcher_finite_callbacks
    pipeline._require_dispatcher_finite_callbacks = lambda _callbacks: None
    try:
        return resources.run_scoped(
            path,
            selected["targets"],
            finite_frontier=selected,
            request_timeout_seconds=request_timeout,
            **callbacks,
        )
    finally:
        pipeline._require_dispatcher_finite_callbacks = original


def test_public_finite_scoped_entry_rejects_unregistered_callback_before_intent(tmp_path):
    selected = frontier()
    path = tmp_path / "state.json"
    called = []
    callbacks = dict(
        prepare=lambda *_: called.append("prepare"),
        interpret=lambda *_: called.append("interpret"),
        exception=lambda *_: called.append("exception"),
        act=lambda *_: called.append("act"),
        capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
    )
    with pytest.raises(ValueError, match="registered production dispatcher"):
        resources.run_scoped(path, selected["targets"], finite_frontier=selected, **callbacks)
    assert called == []
    assert not resources.scoped_root(path).exists()
    with pytest.raises(ValueError, match="registered production dispatcher"):
        pipeline.run_pipeline(
            path,
            selected["targets"],
            coordinator_mode="resource_scoped",
            finite_frontier=selected,
            **callbacks,
        )
    assert called == []
    assert not resources.scoped_root(path).exists()


def test_scoped_finite_work_can_outlast_one_request_window_without_repetition(
    tmp_path, monkeypatch
):
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    seen = []

    def stage(name, result):
        def call(target, context):
            assert context["deadline"] == clock[0] + 100
            seen.append(name)
            clock[0] += 50
            return result

        return call

    path = tmp_path / "state.json"
    selected = frontier()
    result = run_selected(
        path,
        selected,
        now=lambda: clock[0],
        prepare=stage("prepare", {"revision": "head-a"}),
        interpret=stage("interpret", {"substantive_needs": []}),
        act=stage("act", {"status": "completed", "target_complete": True}),
    )
    assert result["targets"]["owner/repo#1"]["status"] == "completed"
    assert result["elapsed_seconds"] == 150
    assert "deadline" not in result
    assert resources.validate_stop(path, result["round_id"], selected["targets"])["valid"]
    run_selected(path, selected, prepare=lambda *_: pytest.fail("completed frontier repeated"))
    assert seen == ["prepare", "interpret", "act"]


@pytest.mark.parametrize("gate,expected", [("cancelled_fn", "cancelled"), ("paused_fn", "paused")])
def test_finite_controls_stop_admission_between_requests(tmp_path, gate, expected):
    stop = [False]

    def prepare(*_):
        stop[0] = True
        return {"revision": "head-a"}

    result = run_selected(
        tmp_path / "state.json",
        frontier(),
        prepare=prepare,
        interpret=lambda *_: pytest.fail("stopped request admitted"),
        **{gate: lambda: stop[0]},
    )
    assert result["targets"]["owner/repo#1"]["status"] == expected


def test_finite_capacity_loss_stops_next_request(tmp_path):
    available = [True]

    def prepare(*_):
        available[0] = False
        return {"revision": "head-a"}

    result = run_selected(
        tmp_path / "state.json",
        frontier(),
        prepare=prepare,
        capacity_fn=lambda: {"io": 1, "low": int(available[0]), "exception": 1},
        interpret=lambda *_: pytest.fail("capacity-blocked provider request admitted"),
    )
    assert result["targets"]["owner/repo#1"]["status"] == "capacity_blocked"


def test_pool_wait_cannot_renew_expired_finite_request(tmp_path, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    waited = []

    @contextmanager
    def slow_pool(_lane, _target, context):
        waited.append("pool")
        original_deadline = context["deadline"]
        clock[0] += 101
        assert context["deadline"] == original_deadline
        yield

    result = run_selected(
        tmp_path / "state.json",
        frontier(),
        pool_lease=slow_pool,
        prepare=lambda *_: pytest.fail("expired request entered after pool wait"),
        now=lambda: clock[0],
    )
    row = result["targets"]["owner/repo#1"]
    assert row["status"] in {"held", "unknown"}
    assert waited == ["pool"]
    assert row["error"]


def test_finite_unknown_survives_restart_without_action_replay(tmp_path):
    path = tmp_path / "state.json"
    selected = frontier()
    calls = []

    def uncertain(*_):
        calls.append("act")
        raise RuntimeError("transport outcome unknown")

    first = run_selected(path, selected, act=uncertain)
    assert first["targets"]["owner/repo#1"]["status"] == "unknown"
    shard = resources.shard_dir(resources.scoped_root(path), "owner/repo#1") / "state.json"
    original = shard.read_bytes()
    second = run_selected(path, selected, act=lambda *_: pytest.fail("UNKNOWN replayed"))
    assert second["targets"]["owner/repo#1"]["scoped_fence"]
    assert shard.read_bytes() == original
    assert calls == ["act"]
    assert resources.unknown_reservations(resources.scoped_root(path))


def test_finite_request_concurrency_stays_within_shared_pool(tmp_path):
    selected = frontier(tuple(f"owner/repo{n}#1" for n in range(6)))
    lock = threading.Lock()
    active = [0]
    peak = [0]

    def prepare(*_):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.02)
        with lock:
            active[0] -= 1
        return {"revision": "head-a"}

    result = run_selected(
        tmp_path / "state.json",
        selected,
        prepare=prepare,
        max_io_workers=2,
        max_low_workers=1,
        max_exception_workers=1,
    )
    assert 1 <= peak[0] <= 2
    assert all(row["status"] == "completed" for row in result["targets"].values())


def test_mixed_case_frontier_keeps_original_keys_and_canonical_lease(tmp_path):
    selected = frontier(("owner/MixedCase#1",))
    path = tmp_path / "state.json"
    result = run_selected(path, selected)
    assert result["targets"]["owner/MixedCase#1"]["status"] == "completed"
    assert result["finite_frontier"] == selected
    assert "deadline" not in result and "run_deadlines" not in result
    assert resources.validate_stop(path, result["round_id"], selected["targets"])["valid"]
    assert not quarantine.scope_snapshot(path)["items"]


def test_finite_census_requires_held_exact_owner_epoch_and_descriptor(tmp_path):
    path = tmp_path / "state.json"
    selected = frontier()
    target = selected["targets"][0]
    root = resources.open_root(path, pipeline_schema=pipeline.SCHEMA)
    lease = resources.acquire(root, target, owner="first", finite_frontier=selected)
    record = {
        "schema": pipeline.SCHEMA,
        "runs": {},
        "carry": {},
        "cache": {},
        "frontiers": {
            selected["frontier_id"]: {
                "schema": "closer-pipeline-frontier/v2",
                "run_id": "finite-run",
                "finite_frontier": selected,
                "items": {
                    target: {
                        "status": "running",
                        "stage": "prepare",
                        "results": {},
                        "scope_lease": {
                            "target": target,
                            "owner": lease.owner,
                            "epoch": lease.epoch,
                        },
                        "last_scheduled": time.time(),
                    }
                },
            }
        },
    }
    try:
        lease.state_path.write_text(json.dumps(record))
        assert not quarantine.scope_snapshot(path)["items"]
        row = record["frontiers"][selected["frontier_id"]]["items"][target]
        row["scope_lease"]["epoch"] += 1
        lease.state_path.write_text(json.dumps(record))
        assert quarantine.scope_snapshot(path)["items"]
        row["scope_lease"]["epoch"] -= 1
        lease.state_path.write_text(json.dumps(record))
    finally:
        lease.release()
    assert quarantine.scope_snapshot(path)["items"]
    unresolved, malformed = resources.scoped_rollback_report(path)
    assert not malformed
    assert target in unresolved
    replacement = resources.acquire(root, target, owner="restarted", finite_frontier=selected)
    try:
        assert quarantine.scope_snapshot(path)["items"]
    finally:
        replacement.release()


def test_hung_request_transport_is_killed_and_not_repeated(tmp_path, monkeypatch):
    pid_file = tmp_path / "request.pid"
    calls = []
    script = (
        "import json,os,pathlib,subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(5)'],"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        "pathlib.Path(sys.argv[1]).write_text(json.dumps({'parent':os.getpid(),'descendant':child.pid})); "
        "time.sleep(5)"
    )
    command = [sys.executable, "-c", script, str(pid_file)]
    original_popen = subprocess.Popen

    def read_pids():
        try:
            pids = json.loads(pid_file.read_text())
        except (FileNotFoundError, ValueError):
            return None
        if (
            set(pids) != {"parent", "descendant"}
            or any(type(pid) is not int or pid <= 1 for pid in pids.values())
            or pids["parent"] == pids["descendant"]
        ):
            return None
        return pids

    def ready_popen(argv, *args, **kwargs):
        process = original_popen(argv, *args, **kwargs)
        if argv == command:
            # Establish mandatory PID evidence before communicate's short
            # timeout starts, rather than relying on scheduler startup speed.
            limit = time.monotonic() + 3
            while read_pids() is None and process.poll() is None and time.monotonic() < limit:
                time.sleep(0.01)
            pids = read_pids()
            assert pids is not None, "owned parent/descendant PID evidence was not published"
            assert pids["parent"] == process.pid
        return process

    monkeypatch.setattr(subprocess, "Popen", ready_popen)

    def prepare(_target, context):
        calls.append("prepare")
        return bounded_run(
            command,
            timeout=min(0.1, context["deadline"] - time.time()),
        )

    path = tmp_path / "state.json"
    selected = frontier()
    started = time.monotonic()
    result = run_selected(path, selected, prepare=prepare)
    assert time.monotonic() - started < 6
    assert result["targets"][selected["targets"][0]]["status"] == "held"
    assert "TimeoutExpired" in result["targets"][selected["targets"][0]]["error"]
    pids = read_pids()
    assert pids is not None
    with pytest.raises(ProcessLookupError):
        os.kill(pids["parent"], 0)  # communicate must reap the direct child.

    def descendant_stopped():
        try:
            os.kill(pids["descendant"], 0)
        except ProcessLookupError:
            return True
        if sys.platform.startswith("linux"):
            # Some container PID1s do not promptly reap orphaned descendants.
            # Z is terminal/non-runnable, not proof that the group is absent;
            # the production pool fence still requires its own absence proof.
            try:
                state = Path(f"/proc/{pids['descendant']}/stat").read_text()
            except FileNotFoundError:
                return True
            return state.rsplit(")", 1)[1].split()[0] == "Z"
        return False

    limit = time.monotonic() + 2
    while not descendant_stopped() and time.monotonic() < limit:
        time.sleep(0.01)
    assert descendant_stopped(), "owned descendant remained runnable after group timeout cleanup"
    run_selected(path, selected, prepare=lambda *_: pytest.fail("timed out request repeated"))
    assert calls == ["prepare"]


@contextmanager
def held_inventory(root):
    lock = root.with_name(root.name + ".inventory.lock")
    script = (
        "import fcntl,pathlib,sys; p=pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True,exist_ok=True); "
        "f=p.open('a+'); fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); sys.stdin.read(1)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(lock)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        yield
    finally:
        child.stdin.close()
        child.wait(timeout=3)
        child.stdout.close()


@pytest.mark.parametrize("inherited", [False, True])
def test_finite_inventory_lock_wait_obeys_original_request(tmp_path, inherited):
    root = resources.scoped_root(tmp_path / "state.json")
    with held_inventory(root):
        started = time.monotonic()
        deadline = time.time() + 0.05
        context = resources.inventory_request(deadline) if inherited else nullcontext()
        with context, pytest.raises(resources.ResourceBlocked, match="finite request window"):
            with resources.inventory_lock(root, deadline=None if inherited else deadline):
                pytest.fail("contended inventory admitted")
        assert time.monotonic() - started < 0.4


def test_finite_scope_check_cannot_wait_past_request(tmp_path):
    path = tmp_path / "state.json"
    root = resources.scoped_root(path)
    calls = []

    @contextmanager
    def blocked_pool(_lane, _target, _context):
        with held_inventory(root):
            yield

    started = time.monotonic()
    result = run_selected(
        path,
        frontier(),
        request_timeout_seconds=0.15,
        pool_lease=blocked_pool,
        prepare=lambda *_: calls.append("prepare"),
    )
    assert time.monotonic() - started < 1.5
    assert calls == []
    assert result["targets"]["owner/repo#1"]["status"] != "completed"


def test_finite_journal_timeout_after_effect_retains_unknown_intent(tmp_path):
    path = tmp_path / "state.json"
    root = resources.scoped_root(path)
    held = []
    effects = []

    def act(*_):
        effects.append("effect")
        lock = held_inventory(root)
        lock.__enter__()
        held.append(lock)
        return {"status": "completed", "target_complete": True}

    try:
        started = time.monotonic()
        result = run_selected(path, frontier(), request_timeout_seconds=0.2, act=act)
        assert time.monotonic() - started < 1.5
        assert result["targets"]["owner/repo#1"]["status"] != "completed"
        assert effects == ["effect"]
        assert resources.unknown_reservations(root)
        state_path = resources.shard_dir(root, "owner/repo#1") / "state.json"
        doc = json.loads(state_path.read_text())
        assert "owner/repo#1" in resources.finite_unresolved(doc)
    finally:
        for lock in held:
            lock.__exit__(None, None, None)
    second = run_selected(path, frontier(), act=lambda *_: pytest.fail("unknown effect replayed"))
    assert second["targets"]["owner/repo#1"]["scoped_fence"]


def test_cleanup_uncertainty_fences_lane_and_profile_pools_until_group_absent(tmp_path):
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    roots = [tmp_path / "lane-pools", tmp_path / "profile-pools"]
    fence = {
        "process_group": child.pid,
        "run_id": "owned-run-1",
        "agent": "cursor",
        "profile_id": "cursor-low",
        "process_birth": "Tue Oct 20 05:35:00 2026",
        "frontier_id": "frozen-frontier-1",
        "target": "owner/repo#1",
        "cleanup_terminal": False,
        "cleanup_error": {"type": "PermissionError", "errno": 1},
    }
    try:
        with pytest.raises(resources.FinitePoolUncertain):
            with resources.pool_slot(
                roots[0],
                "low",
                1,
                deadline=time.time() + 1,
                finite_frontier=True,
                reservation={
                    "reservation_id": "lane-r1",
                    "target": fence["target"],
                    "frontier_id": fence["frontier_id"],
                    "agent": "closer",
                    "profile_id": "low",
                },
            ):
                with resources.pool_slot(
                    roots[1],
                    "profile-sha",
                    1,
                    deadline=time.time() + 1,
                    finite_frontier=True,
                    reservation={
                        "reservation_id": "profile-r1",
                        "target": fence["target"],
                        "frontier_id": fence["frontier_id"],
                        "agent": "cursor",
                        "profile_id": "cursor-low",
                    },
                ):
                    raise resources.FinitePoolUncertain(fence)

        # A distinct target through either the new finite mode or a legacy/scoped
        # caller cannot reuse capacity while the exact original group is live.
        for pool_root, lane in ((roots[0], "low"), (roots[1], "profile-sha")):
            with pytest.raises(resources.ResourceBlocked, match="no .* pool slot"):
                with resources.pool_slot(pool_root, lane, 1, deadline=time.time() + 0.2):
                    pytest.fail("uncertain pool slot was reused")
        for pool_root, lane in ((roots[0], "low"), (roots[1], "profile-sha")):
            slot = pool_root / "pools" / lane / "slot-0.json"
            record = json.loads(slot.read_text())
            assert record["schema"] == "finite-pool-uncertainty/v1"
            assert record["status"] == "uncertain"
            assert record["target"] == "owner/repo#1"
            assert record["frontier_id"] == "frozen-frontier-1"

        # An uncertain slot consumes one worker, not the whole lane.
        with resources.pool_slot(roots[0], "low", 2, deadline=time.time() + 1):
            pass

        os.killpg(child.pid, 9)
        child.wait(timeout=2)
        for pool_root, lane in ((roots[0], "low"), (roots[1], "profile-sha")):
            with resources.pool_slot(pool_root, lane, 1, deadline=time.time() + 1):
                pass
            record = json.loads((pool_root / "pools" / lane / "slot-0.json").read_text())
            assert record["status"] == "resolved"
            assert record["resolution"] == "process_group_absent"
    finally:
        if child.poll() is None:
            os.killpg(child.pid, 9)
            child.wait(timeout=2)


def test_finite_pool_prelaunch_record_survives_coordinator_interruption(tmp_path):
    root = tmp_path / "pool"
    reservation = {
        "reservation_id": "reserve-before-spawn",
        "target": "owner/repo#2",
        "frontier_id": "frozen-frontier-2",
        "agent": "cursor",
        "profile_id": "cursor-low",
    }
    with pytest.raises(KeyboardInterrupt):
        with resources.pool_slot(
            root,
            "low",
            1,
            deadline=time.time() + 1,
            finite_frontier=True,
            reservation=reservation,
        ):
            record = json.loads((root / "pools" / "low" / "slot-0.json").read_text())
            assert record["status"] == "starting"
            raise KeyboardInterrupt("simulated coordinator interruption")

    # There is no PID/birth proof after interruption, so a later target cannot
    # infer that nothing started or clear the slot.
    with pytest.raises(resources.ResourceBlocked, match="no low pool slot"):
        with resources.pool_slot(root, "low", 1, deadline=time.time() + 0.1):
            pytest.fail("unresolved prelaunch reservation was cleared")


@pytest.mark.parametrize("replacement_run", ["original-request", "later-request"])
def test_prepared_starting_request_cannot_be_replaced(tmp_path, replacement_run):
    root = tmp_path / "pool"
    reservation = {
        "reservation_id": "starting-identity",
        "target": "owner/repo#1",
        "frontier_id": "frozen-1",
        "agent": "closer",
        "profile_id": "io",
    }
    original = {
        "run_id": "original-request",
        "agent": "github",
        "profile_id": "github-api",
        "finite_frontier_id": reservation["frontier_id"],
        "finite_frontier_target": reservation["target"],
    }
    with resources.pool_slot(
        root, "io", 1, deadline=time.time() + 1, finite_frontier=True, reservation=reservation
    ):
        resources.finite_pool_prepare_request(original)
        path = root / "pools" / "io" / "slot-0.json"
        before = path.read_bytes()
        with pytest.raises(resources.ResourceBlocked, match="reusable"):
            resources.finite_pool_prepare_request({**original, "run_id": replacement_run})
        resources.finite_pool_no_start()
        assert path.read_bytes() == before


@pytest.mark.parametrize(
    "failure", ["pause", "expiry", "inventory", "before-request", "after-request"]
)
def test_finite_callback_exception_releases_only_never_prepared_slot(
    tmp_path, monkeypatch, failure
):
    selected = frontier()
    target = selected["targets"][0]
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    paused = [False]
    pool_root = tmp_path / "probe-pools"
    path = tmp_path / "state.json"
    reservation = {
        "reservation_id": "callback-entry",
        "target": target,
        "frontier_id": selected["frontier_id"],
        "agent": "closer",
        "profile_id": "io",
    }

    @contextmanager
    def controlled_pool(_lane, _target, context):
        with resources.pool_slot(
            pool_root,
            "io",
            1,
            deadline=context["deadline"],
            finite_frontier=True,
            reservation=reservation,
        ):
            if failure == "pause":
                paused[0] = True
            elif failure == "expiry":
                clock[0] += 1
            if failure == "inventory":
                with held_inventory(resources.scoped_root(path)):
                    yield
            else:
                yield

    def prepare(*_):
        if failure == "after-request":
            resources.finite_pool_prepare_request(
                {
                    "run_id": "possible-spawn",
                    "agent": "github",
                    "profile_id": "github-api",
                    "finite_frontier_id": selected["frontier_id"],
                    "finite_frontier_target": target,
                }
            )
        raise RuntimeError("synthetic callback refusal")

    run_selected(
        path,
        selected,
        request_timeout_seconds=0.1,
        pool_lease=controlled_pool,
        paused_fn=lambda: paused[0],
        now=lambda: clock[0],
        prepare=prepare,
    )
    record = json.loads((pool_root / "pools" / "io" / "slot-0.json").read_text())
    if failure == "after-request":
        assert record["status"] == "starting" and record["run_id"] == "possible-spawn"
        with pytest.raises(resources.ResourceBlocked):
            with resources.pool_slot(pool_root, "io", 1, deadline=time.time() + 0.05):
                pytest.fail("possibly spawned request reservation cleared")
    else:
        assert record["status"] == "resolved" and record["run_id"] is None
        assert record["resolution"] == "trusted_no_spawn"
        with resources.pool_slot(pool_root, "io", 1, deadline=time.time() + 0.05):
            pass


def test_finite_owned_transport_updates_and_resolves_prelaunch_fence(tmp_path):
    root = tmp_path / "pool"
    reservation = {
        "reservation_id": "reserve-owned-run",
        "target": "owner/repo#3",
        "frontier_id": "frozen-frontier-3",
        "agent": "fixture",
        "profile_id": "fixture-low",
    }
    observation = {
        "run_id": "owned-transport-run",
        "agent": "fixture",
        "profile_id": "fixture-low",
        "finite_frontier_id": reservation["frontier_id"],
        "finite_frontier_target": reservation["target"],
        "provider_argv": [sys.executable, "-c", "pass"],
    }
    with resources.pool_slot(
        root,
        "low",
        1,
        deadline=time.time() + 2,
        finite_frontier=True,
        reservation=reservation,
    ):
        result = dispatcher.run_observed_offload(
            [sys.executable, "-c", "print('bounded fixture', flush=True)"],
            callback=resources.finite_pool_process_started,
            finished_callback=resources.finite_pool_process_finished,
            starting_callback=resources.finite_pool_prepare_request,
            observation=observation,
            cwd=str(tmp_path),
            timeout=2,
            stdin=subprocess.DEVNULL,
        )
        assert result.returncode == 0 and "bounded fixture" in result.stdout
    record = json.loads((root / "pools" / "low" / "slot-0.json").read_text())
    assert record["status"] == "resolved"
    assert record["run_id"] == "owned-transport-run"
    assert record["process_group"] > 1
    assert record["process_birth"]


def test_sequential_owned_requests_rearm_same_lane_and_reject_stale_finish(tmp_path):
    root = tmp_path / "pool"
    reservation = {
        "reservation_id": "sequential-lane",
        "target": "owner/repo#4",
        "frontier_id": "frozen-frontier-4",
        "agent": "fixture",
        "profile_id": "fixture-low",
    }
    with resources.pool_slot(
        root,
        "low",
        1,
        deadline=time.time() + 3,
        finite_frontier=True,
        reservation=reservation,
    ):
        for index in range(2):
            observation = {
                "run_id": f"owned-sequential-{index}",
                "agent": "fixture",
                "profile_id": "fixture-low",
                "finite_frontier_id": reservation["frontier_id"],
                "finite_frontier_target": reservation["target"],
            }
            resources.finite_pool_prepare_request(observation)
            result = dispatcher.run_observed_offload(
                [sys.executable, "-c", "print('sequential', flush=True)"],
                callback=resources.finite_pool_process_started,
                finished_callback=resources.finite_pool_process_finished,
                observation=observation,
                cwd=str(tmp_path),
                timeout=2,
                stdin=subprocess.DEVNULL,
            )
            assert result.returncode == 0
            assert (
                json.loads((root / "pools" / "low" / "slot-0.json").read_text())["run_id"]
                == observation["run_id"]
            )
        stale = {**observation, "run_id": "stale-run", "process_group": 99999999, "pid": 99999999}
        with pytest.raises(resources.ResourceBlocked, match="no matching pool reservation"):
            resources.finite_pool_process_finished(stale)
    record = json.loads((root / "pools" / "low" / "slot-0.json").read_text())
    assert record["status"] == "resolved"
    assert record["run_id"] == "owned-sequential-1"


def test_birth_probe_failure_keeps_starting_fence_until_exact_group_absence(tmp_path):
    root = tmp_path / "pool"
    reservation = {
        "reservation_id": "birth-uncertain",
        "target": "owner/repo#5",
        "frontier_id": "frozen-frontier-5",
        "agent": "closer-dispatcher",
        "profile_id": "io",
    }
    request = {
        "run_id": "birth-unknown-run",
        "agent": "github",
        "profile_id": "github-artifact",
        "finite_frontier_id": reservation["frontier_id"],
        "finite_frontier_target": reservation["target"],
    }
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    with resources.pool_slot(
        root,
        "io",
        1,
        deadline=time.time() + 2,
        finite_frontier=True,
        reservation=reservation,
    ):
        resources.finite_pool_prepare_request(request)
        preflight = {
            **request,
            "source": "trusted-artifact-runner/v1",
            "pid": child.pid,
            "process_group": child.pid,
        }
        with pytest.raises(resources.ResourceBlocked, match="identity is incomplete"):
            resources.finite_pool_process_started(preflight)
        with pytest.raises(resources.ResourceBlocked, match="still present"):
            resources.finite_pool_process_finished(preflight)
        assert (
            json.loads((root / "pools" / "io" / "slot-0.json").read_text())["status"] == "starting"
        )
        os.killpg(child.pid, 9)
        child.wait(timeout=2)
        resources.finite_pool_process_finished(preflight)
    record = json.loads((root / "pools" / "io" / "slot-0.json").read_text())
    assert record["status"] == "resolved"
    assert record["run_id"] == request["run_id"]
