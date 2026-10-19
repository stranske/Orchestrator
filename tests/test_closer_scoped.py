"""Resource-scoped coordinator mode, exercised through the real run_pipeline in separate processes."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

import closer_pipeline
import closer_resources

WORKER = Path(__file__).with_name("closer_scoped_worker.py")


def marker(directory: Path, target: str) -> Path:
    return directory / hashlib.sha256(target.encode()).hexdigest()[:8]


def spawn(spec: dict) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(WORKER), json.dumps(spec)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def finish(proc: subprocess.Popen, timeout: float = 40) -> dict:
    out, err = proc.communicate(timeout=timeout)
    assert out.strip(), f"worker produced no receipt (rc={proc.returncode}): {err[-800:]}"
    return json.loads(out)


def wait_for(path: Path, timeout: float = 15) -> None:
    end = time.time() + timeout
    while not path.exists():
        assert time.time() < end, f"timed out waiting for {path}"
        time.sleep(0.02)


def acts(log: Path) -> list[str]:
    if not log.exists():
        return []
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    return [r["t"] for r in rows if r["e"] == "act"]


def test_default_mode_is_legacy_and_creates_no_scoped_state(tmp_path):
    state = tmp_path / "s.json"
    receipt = finish(spawn({"state": str(state), "targets": ["o/r#1"]}))
    assert receipt["targets"]["o/r#1"]["status"] == "completed"
    assert "mode" not in receipt and state.exists()
    assert not closer_resources.scoped_root(state).exists()


def test_invalid_mode_is_refused(tmp_path):
    receipt = finish(spawn({"state": str(tmp_path / "s.json"), "targets": ["a"], "mode": "turbo"}))
    assert "coordinator mode must be one of" in receipt["raised"]


def test_disjoint_coordinators_progress_concurrently(tmp_path):
    """Each act waits for the OTHER process to be inside act: only possible with no global lock."""
    state, enter = tmp_path / "s.json", tmp_path / "enter"
    enter.mkdir()
    common = {"state": str(state), "mode": "resource_scoped", "act_enter_dir": str(enter)}
    a = spawn(
        {**common, "targets": ["o/r#A"], "act_hold_for": {"o/r#A": str(marker(enter, "o/r#B"))}}
    )
    b = spawn(
        {**common, "targets": ["o/r#B"], "act_hold_for": {"o/r#B": str(marker(enter, "o/r#A"))}}
    )
    ra, rb = finish(a), finish(b)
    assert ra["targets"]["o/r#A"]["status"] == "completed"
    assert rb["targets"]["o/r#B"]["status"] == "completed"
    assert ra["scoped"]["fenced_targets"] == rb["scoped"]["fenced_targets"] == []


def test_same_target_is_exclusive_and_loser_commits_nothing(tmp_path):
    state, enter, release, log = (
        tmp_path / "s.json",
        tmp_path / "enter",
        tmp_path / "go",
        tmp_path / "log",
    )
    enter.mkdir()
    common = {"state": str(state), "mode": "resource_scoped", "targets": ["o/r#T"], "log": str(log)}
    owner = spawn({**common, "act_enter_dir": str(enter), "act_hold_for": {"o/r#T": str(release)}})
    wait_for(marker(enter, "o/r#T"))
    loser = finish(spawn(common))
    assert loser["targets"]["o/r#T"]["deferred_reason"] == "resource_contested"
    assert loser["scoped"]["fenced_targets"] == ["o/r#T"]
    release.touch()
    assert finish(owner)["targets"]["o/r#T"]["status"] == "completed"
    assert acts(log) == ["o/r#T"]  # the loser never reached act


def test_stale_or_expired_lease_cannot_commit(tmp_path):
    root = closer_resources.open_root(tmp_path / "s.json", pipeline_schema=closer_pipeline.SCHEMA)
    clock = [1000.0]
    lease = closer_resources.acquire(
        root, "o/r#1", owner="w1", expires_at=1100.0, now=lambda: clock[0]
    )
    assert lease is not None
    assert closer_resources.acquire(root, "o/r#1", owner="w2", expires_at=9e9) is None  # live owner
    with closer_resources.fenced(lease, lambda: clock[0]):
        closer_pipeline._save(lease.state_path, closer_pipeline._empty())
        assert lease.state_path.exists()
        before = lease.state_path.read_bytes()
        record = lease.directory / "lease.json"
        doc = json.loads(record.read_text())
        record.write_text(json.dumps({**doc, "epoch": doc["epoch"] + 1, "owner": "w2"}))
        with pytest.raises(closer_resources.StaleLease, match="superseded"):
            closer_pipeline._save(lease.state_path, {**closer_pipeline._empty(), "x": 1})
        record.write_text(json.dumps(doc))
        clock[0] = 1101.0
        with pytest.raises(closer_resources.StaleLease, match="expired"):
            closer_pipeline._save(lease.state_path, {**closer_pipeline._empty(), "x": 2})
    assert lease.state_path.read_bytes() == before
    lease.release()
    with pytest.raises(closer_resources.StaleLease, match="released"):
        lease.check()
    again = closer_resources.acquire(root, "o/r#1", owner="w3", expires_at=9e9)
    assert again is not None and again.epoch == lease.epoch + 1  # takeover only after release
    again.release()


def test_crash_mid_act_keeps_unknown_fenced_without_blocking_disjoint(tmp_path):
    state, log = tmp_path / "s.json", tmp_path / "log"
    base = {"state": str(state), "mode": "resource_scoped", "log": str(log)}
    crashed = spawn({**base, "targets": ["o/r#X"], "crash_in_act": ["o/r#X"]})
    crashed.communicate(timeout=30)
    assert crashed.returncode == 9
    assert closer_resources.scoped_unresolved(state) == {"o/r#X": "running"}
    receipt = finish(spawn({**base, "targets": ["o/r#X", "o/r#Y"]}))
    assert receipt["targets"]["o/r#Y"]["status"] == "completed"
    assert receipt["targets"]["o/r#X"]["status"] != "completed"
    assert acts(log).count("o/r#X") == 1  # the interrupted action is never blindly repeated
    assert closer_resources.scoped_unresolved(state).get("o/r#X") in {"running", "uncertain"}


def test_github_outage_on_act_still_leaves_local_preparation_and_disjoint_progress(tmp_path):
    state = tmp_path / "s.json"
    receipt = finish(
        spawn(
            {
                "state": str(state),
                "mode": "resource_scoped",
                "targets": ["o/r#out", "o/r#ok"],
                "outage_act": ["o/r#out"],
            }
        )
    )
    assert receipt["targets"]["o/r#ok"]["status"] == "completed"
    out = receipt["targets"]["o/r#out"]
    assert out["status"] != "completed"
    assert out["results"]["prepare"]["identity"] == "o/r#out"
    shard = closer_resources.shard_dir(closer_resources.scoped_root(state), "o/r#out")
    saved = json.loads((shard / "state.json").read_text())
    assert any(
        "prepare" in row.get("results", {})
        for run in saved["runs"].values()
        for row in run["targets"].values()
    )


def test_legacy_ledger_is_untouched_and_its_unknowns_stay_fenced(tmp_path):
    state, log = tmp_path / "s.json", tmp_path / "log"
    crashed = spawn(
        {"state": str(state), "targets": ["o/r#L"], "crash_in_act": ["o/r#L"], "log": str(log)}
    )
    crashed.communicate(timeout=30)
    ledger = state.read_bytes()
    assert json.loads(ledger)["runs"]
    receipt = finish(
        spawn(
            {
                "state": str(state),
                "mode": "resource_scoped",
                "targets": ["o/r#L", "o/r#M"],
                "log": str(log),
            }
        )
    )
    assert receipt["targets"]["o/r#L"]["deferred_reason"] == "legacy_running_outcome"
    assert receipt["targets"]["o/r#M"]["status"] == "completed"
    assert state.read_bytes() == ledger  # byte-for-byte, no cleared unknown
    assert acts(log).count("o/r#L") == 1


def test_unreadable_legacy_ledger_fences_everything(tmp_path):
    state = tmp_path / "s.json"
    state.write_text("{not json")
    receipt = finish(spawn({"state": str(state), "mode": "resource_scoped", "targets": ["a", "b"]}))
    assert {r["deferred_reason"] for r in receipt["targets"].values()} == {
        "legacy_state_unreadable"
    }
    assert state.read_text() == "{not json"


def test_incompatible_contract_and_version_pin_are_refused_not_migrated(tmp_path):
    state = tmp_path / "s.json"
    ok = finish(spawn({"state": str(state), "mode": "resource_scoped", "targets": ["a"]}))
    assert ok["targets"]["a"]["status"] == "completed"
    pin = {"layout": closer_resources.LAYOUT, "pipeline_schema": closer_pipeline.SCHEMA}
    pinned = finish(
        spawn({"state": str(state), "mode": "resource_scoped", "targets": ["a"], "pin": pin})
    )
    assert "targets" in pinned
    bad_pin = {**pin, "layout": 99}
    refused = finish(
        spawn({"state": str(state), "mode": "resource_scoped", "targets": ["a"], "pin": bad_pin})
    )
    assert "IncompatibleStateContract" in refused["raised"]
    contract = closer_resources.scoped_root(state) / "contract.json"
    before = contract.read_text()
    contract.write_text(json.dumps({"schema": "closer-scoped-state/v2", "layout": 2}))
    changed = contract.read_text()
    refused = finish(spawn({"state": str(state), "mode": "resource_scoped", "targets": ["a"]}))
    assert "IncompatibleStateContract" in refused["raised"]
    assert contract.read_text() == changed != before  # left as found
    assert closer_pipeline.scoped_capability()["version_pin"] is True
    assert closer_pipeline.scoped_capability()["proven_runtime_promotion"] is False


def test_legacy_mode_rejects_a_contract_pin(tmp_path):
    receipt = finish(
        spawn({"state": str(tmp_path / "s.json"), "targets": ["a"], "pin": {"layout": 1}})
    )
    assert "resource_scoped mode only" in receipt["raised"]


def test_measured_wall_clock_versus_legacy_two_coordinators(tmp_path, capsys):
    """Records elapsed and lease wait from real processes. Asserts completion, NOT a speedup:
    the fixture's act is a sleep, so the numbers say nothing about real closer throughput."""
    out = {}
    for mode in ("legacy", "resource_scoped"):
        state = tmp_path / f"{mode}.json"
        started = time.time()
        procs = [
            spawn(
                {
                    "state": str(state),
                    "mode": mode,
                    "targets": [f"o/r#{mode}{i}-{j}" for j in range(2)],
                    "act_sleep": 0.3,
                }
            )
            for i in range(2)
        ]
        receipts = [finish(p) for p in procs]
        done = sum(
            1 for r in receipts for row in r["targets"].values() if row.get("status") == "completed"
        )
        out[mode] = {
            "wall_seconds": round(time.time() - started, 3),
            "completed_eligible": done,
            "max_lease_wait_seconds": max(
                (r.get("scoped", {}).get("max_lease_wait_seconds", 0.0) for r in receipts)
            ),
        }
        assert done == 4, out[mode]
    with capsys.disabled():
        print(
            "\nclosers-scoped-measurement (sleep fixture, not throughput evidence):",
            json.dumps(out),
        )


# ---- integration seams: rollback, resource keys, aggregate validation, pools, strict state


def intervals(log: Path) -> list[tuple[str, float, float]]:
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    out = []
    for enter in (r for r in rows if r["e"] == "act_enter"):
        leave = next(r for r in rows if r["e"] == "act_exit" and r["t"] == enter["t"])
        out.append((enter["t"], enter["at"], leave["at"]))
    return out


def test_legacy_rollback_refuses_scoped_unknown_and_allows_disjoint(tmp_path):
    state, log = tmp_path / "s.json", tmp_path / "log"
    base = {"state": str(state), "log": str(log)}
    spawn(
        {**base, "mode": "resource_scoped", "targets": ["o/r#5"], "crash_in_act": ["o/r#5"]}
    ).communicate(timeout=30)
    assert closer_resources.scoped_unresolved(state) == {"o/r#5": "running"}
    receipt = finish(spawn({**base, "targets": ["O/R#5", "o/r#6"]}))  # default (legacy) mode
    assert receipt["targets"]["o/r#6"]["status"] == "completed"
    assert receipt["targets"]["O/R#5"]["deferred_reason"] == "scoped_running_outcome"
    assert acts(log) == ["o/r#5", "o/r#6"]  # legacy never acted on the affected target
    only = finish(spawn({**base, "targets": ["O/R#5"]}))
    assert only["mode"] == "legacy_refused_by_scoped_state" and only["run_id"] is None


def test_malformed_shard_fails_closed_for_every_legacy_target(tmp_path):
    state, log = tmp_path / "s.json", tmp_path / "log"
    finish(spawn({"state": str(state), "mode": "resource_scoped", "targets": ["o/r#1"]}))
    shard = closer_resources.shard_dir(closer_resources.scoped_root(state), "o/r#1")
    (shard / "state.json").write_text("{broken")
    receipt = finish(spawn({"state": str(state), "log": str(log), "targets": ["o/r#8", "o/r#9"]}))
    assert {r["deferred_reason"] for r in receipt["targets"].values()} == {
        "scoped_shard_unreadable"
    }
    assert not log.exists() and not state.exists()  # legacy neither acted nor wrote


def test_two_ids_for_one_branch_never_act_concurrently(tmp_path):
    state, log = tmp_path / "s.json", tmp_path / "log"
    common = {"state": str(state), "mode": "resource_scoped", "log": str(log), "act_sleep": 0.4}
    keys = {"o/r#1": ["branch:O/R@feat"], "o/r#2": ["branch:o/r@feat"]}  # case-folded alias
    procs = [spawn({**common, "targets": [t], "keys": keys}) for t in keys]
    receipts = [finish(p) for p in procs]
    assert any(
        row["status"] == "completed" for r in receipts for row in r["targets"].values()
    ), receipts
    # A contender may hold until a later cadence: normal callback return is not
    # sufficient to release the first operation's durable reservation.
    events = sorted(intervals(log), key=lambda i: i[1])
    assert events
    assert all(a[2] <= b[1] for a, b in zip(events, events[1:])), "acts on one branch overlapped"


def test_same_repo_acts_serialise_by_default_but_reasoning_is_not_blocked(tmp_path):
    state, log = tmp_path / "s.json", tmp_path / "log"
    common = {"state": str(state), "mode": "resource_scoped", "log": str(log), "act_sleep": 0.3}
    procs = [spawn({**common, "targets": [f"o/r#{n}"]}) for n in (11, 12)]
    assert all(finish(p)["targets"] for p in procs)
    a, b = sorted(intervals(log), key=lambda i: i[1])
    assert a[2] <= b[1]
    prepares = [json.loads(x)["at"] for x in log.read_text().splitlines() if '"prepare"' in x]
    assert len(prepares) == 2


def test_crash_leaves_durable_unknown_blocking_only_that_resource(tmp_path):
    state, log = tmp_path / "s.json", tmp_path / "log"
    base = {"state": str(state), "mode": "resource_scoped", "log": str(log)}
    keys = {"o/r#1": ["branch:o/r@feat"], "o/r#2": ["branch:o/r@feat"], "o/r#3": []}
    spawn({**base, "targets": ["o/r#1"], "keys": keys, "crash_in_act": ["o/r#1"]}).communicate(
        timeout=30
    )
    root = closer_resources.scoped_root(state)
    assert "branch:o/r@feat" in closer_resources.unknown_reservations(root)
    receipt = finish(spawn({**base, "targets": ["o/r#2", "o/r#3"], "keys": keys}))
    assert receipt["targets"]["o/r#3"]["status"] == "completed"
    assert receipt["targets"]["o/r#2"]["status"] != "completed"
    assert acts(log).count("o/r#2") == 0  # blocked at the guard: the effect never started
    assert [t for t, *_ in intervals_safe(log)].count("o/r#2") == 0
    assert receipt["scoped"]["unknown_reservations"]["branch:o/r@feat"] == "o/r#1"
    assert receipt["scoped"]["unknown_reservations_drainable"] == 0  # o/r#1 is not assigned here


def intervals_safe(log):
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    return [(r["t"], r["at"], r["at"]) for r in rows if r["e"] == "act_exit"]


def test_unknown_reservation_drains_when_owner_completes(tmp_path):
    root = closer_resources.open_root(tmp_path / "s.json", pipeline_schema=closer_pipeline.SCHEMA)
    with pytest.raises(RuntimeError):
        with closer_resources.reserve(
            root, ["pr:O/R#1", "repo:o/r"], target="o/r#1", owner="w", deadline=9e9
        ):
            raise RuntimeError("effect outcome unknown")
    assert closer_resources.unknown_reservations(root) == {
        "pr:o/r#1": "o/r#1"
    }  # repo key: no intent
    with pytest.raises(closer_resources.ResourceBlocked, match="unresolved effect"):
        with closer_resources.reserve(root, ["pr:o/r#1"], target="o/r#2", owner="x", deadline=9e9):
            pytest.fail("effect must not start")
    assert closer_resources.release_target(root, "O/R#1", owner="w") == ["pr:o/r#1"]
    with closer_resources.reserve(root, ["pr:o/r#1"], target="o/r#2", owner="x", deadline=9e9):
        pass


def test_reservations_are_taken_in_lexical_order_and_bad_keys_refused(tmp_path):
    root = closer_resources.open_root(tmp_path / "s.json", pipeline_schema=closer_pipeline.SCHEMA)
    with pytest.raises(ValueError):
        closer_resources.normalize_key("nonsense")
    with pytest.raises(ValueError):
        closer_resources.normalize_key("pr:not-a-pr")
    assert closer_resources.normalize_key("issue:Ow/Re#007") == "issue:ow/re#7"
    assert closer_resources.normalize_key("branch:Ow/Re@Feat") == "branch:ow/re@Feat"
    with closer_resources.reserve(
        root, ["repo:b/b", "repo:a/a"], target="a/a#1", owner="w", deadline=9e9
    ):
        pass


def test_scoped_validate_stop_binds_each_round_and_rejects_empty_or_fenced(tmp_path):
    state = tmp_path / "s.json"
    kw = dict(
        prepare=lambda t, c: {
            "revision": "r",
            "identity": t,
            "semantic_cache_key": "closer-evidence-semantic/v1:" + "a" * 64,
        },
        interpret=lambda t, c: {"substantive_needs": [{"kind": "merge"}]},
        act=lambda t, c: {"status": "completed", "target_complete": True},
        exception=lambda t, c: {"reviewed": True},
        capacity_fn=lambda: {"io": 8, "low": 4, "exception": 2},
    )
    r1 = closer_pipeline.run_pipeline(
        state, ["o/r#1"], coordinator_mode="resource_scoped", deadline=time.time() + 30, **kw
    )
    r2 = closer_pipeline.run_pipeline(
        state, ["o/r#2"], coordinator_mode="resource_scoped", deadline=time.time() + 30, **kw
    )
    assert r1["round_id"] != r2["round_id"] and set(r1["run_ids"]) == {"o/r#1"}
    v1 = closer_pipeline.validate_stop_scoped(state, r1["round_id"], ["o/r#1"])
    assert v1["valid"] and v1["run_ids"] == r1["run_ids"], v1
    assert not closer_pipeline.validate_stop_scoped(state, r1["round_id"], ["o/r#2"])["valid"]
    assert closer_pipeline.validate_stop_scoped(state, "0" * 32)["reasons"][0].startswith(
        "invalid_round_receipt"
    )
    assert closer_pipeline.validate_stop_scoped(state, "latest")["reasons"] == ["invalid_round_id"]
    # a later run of the same shard cannot stand in for round 1's recorded run id
    r3 = closer_pipeline.run_pipeline(
        state, ["o/r#1"], coordinator_mode="resource_scoped", deadline=time.time() + 30, **kw
    )
    assert closer_pipeline.validate_stop_scoped(state, r1["round_id"])["run_ids"] == r1["run_ids"]
    assert r3["round_id"] != r1["round_id"]
    # fenced target: no run id -> invalid, continuation required
    shard = closer_resources.shard_dir(closer_resources.scoped_root(state), "o/r#9")
    shard.mkdir(parents=True)
    (shard / "lease.json").write_text("{bad")
    r4 = closer_pipeline.run_pipeline(
        state, ["o/r#9"], coordinator_mode="resource_scoped", deadline=time.time() + 30, **kw
    )
    v4 = closer_pipeline.validate_stop_scoped(state, r4["round_id"])
    assert (
        not v4["valid"]
        and v4["continuation_required"]
        and "fenced_target:o/r#9:corrupt_shard" in v4["reasons"]
    )


def test_pool_bounds_hold_across_processes(tmp_path):
    state, log = tmp_path / "s.json", tmp_path / "log"
    common = {
        "state": str(state),
        "mode": "resource_scoped",
        "log": str(log),
        "act_sleep": 0.3,
        "max_io": 1,
    }
    procs = [spawn({**common, "targets": [f"p/q#{n}"]}) for n in (1, 2)]
    # distinct repos so only the shared io pool (bound 1) can order them
    for p in procs:
        assert finish(p)["scoped"]["fenced_targets"] == []
    a, b = sorted(intervals(log), key=lambda i: i[1])
    assert a[2] <= b[1], "two processes were inside the io pool at once"


def test_malformed_prior_lease_is_not_epoch_one_and_state_without_lease_is_refused(tmp_path):
    root = closer_resources.open_root(tmp_path / "s.json", pipeline_schema=closer_pipeline.SCHEMA)
    lease = closer_resources.acquire(root, "o/r#1", owner="a", expires_at=9e9)
    record = lease.directory / "lease.json"
    lease.release()
    for bad in (
        "{oops",
        json.dumps({"epoch": 0, "target": "o/r#1"}),
        json.dumps({"epoch": True, "target": "o/r#1"}),
        json.dumps({"epoch": 3, "target": "someone/else#1"}),
        "[]",
    ):
        record.write_text(bad)
        with pytest.raises(closer_resources.CorruptShard):
            closer_resources.acquire(root, "o/r#1", owner="b", expires_at=9e9)
        assert record.read_text() == bad  # fence evidence left untouched
    record.unlink()
    (lease.directory / "state.json").write_text("{}")
    with pytest.raises(closer_resources.CorruptShard, match="lease record is missing"):
        closer_resources.acquire(root, "o/r#1", owner="b", expires_at=9e9)
    # the lock is not left held by the refusals
    (lease.directory / "state.json").unlink()
    assert closer_resources.acquire(root, "o/r#1", owner="b", expires_at=9e9).epoch == 1


def test_identity_is_canonical_and_strict(tmp_path):
    assert closer_resources.canonical_target("Ow/Re#05") == "ow/re#5"
    for bad in (" o/r#1", "o/r#1\n", ""):
        with pytest.raises(ValueError):
            closer_resources.canonical_target(bad)
    r = closer_resources.shard_dir
    root = tmp_path / "x"
    assert r(root, "Ow/Re#1") == r(root, "ow/re#1")
    with pytest.raises(ValueError, match="canonically"):
        closer_resources.run_scoped(
            tmp_path / "s.json",
            ["Ow/Re#1", "ow/re#1"],
            prepare=None,
            interpret=None,
            act=None,
            exception=None,
            capacity_fn=None,
            deadline=time.time() + 5,
        )


def test_incompatible_pin_creates_no_root(tmp_path):
    state = tmp_path / "s.json"
    refused = finish(
        spawn(
            {
                "state": str(state),
                "mode": "resource_scoped",
                "targets": ["a"],
                "pin": {"layout": 99},
            }
        )
    )
    assert "IncompatibleStateContract" in refused["raised"]
    assert not closer_resources.scoped_root(state).exists()


def test_concurrent_fences_on_distinct_shards_do_not_interfere(tmp_path):
    import threading

    root = closer_resources.open_root(tmp_path / "s.json", pipeline_schema=closer_pipeline.SCHEMA)
    leases = [
        closer_resources.acquire(root, f"o/r#{n}", owner="w", expires_at=9e9) for n in range(8)
    ]
    errors, barrier = [], threading.Barrier(8)

    def go(lease):
        try:
            with closer_resources.fenced(lease):
                barrier.wait(timeout=10)
                for _ in range(50):
                    closer_resources.fence_for(lease.state_path)()
                    closer_pipeline._save(lease.state_path, closer_pipeline._empty())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(item,)) for item in leases]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and not closer_resources.SAVE_FENCES
    with closer_resources.fenced(leases[0]):
        with pytest.raises(closer_resources.StaleLease, match="already fenced"):
            with closer_resources.fenced(leases[0]):
                pass
    [item.release() for item in leases]


def test_same_target_cannot_overwrite_historical_unknown(tmp_path):
    root = closer_resources.open_root(tmp_path / "s", pipeline_schema=closer_pipeline.SCHEMA)
    with pytest.raises(RuntimeError):
        with closer_resources.reserve(
            root, ["branch:o/r@shared"], target="o/r#1", owner="first", deadline=9e9
        ):
            raise RuntimeError("uncertain")
    with pytest.raises(closer_resources.ResourceBlocked):
        with closer_resources.reserve(
            root, ["branch:o/r@shared"], target="o/r#1", owner="second", deadline=9e9
        ):
            pytest.fail("unknown reservation overwritten")
    assert closer_resources.release_target(root, "o/r#1", owner="second") == []
    assert closer_resources.unknown_reservations(root) == {"branch:o/r@shared": "o/r#1"}


def test_callback_return_does_not_release_before_durable_completion(tmp_path):
    state = tmp_path / "s"
    common = dict(
        coordinator_mode="resource_scoped",
        deadline=time.time() + 30,
        prepare=lambda t, c: {
            "revision": "r",
            "identity": t,
            "semantic_cache_key": "closer-evidence-semantic/v1:" + "a" * 64,
        },
        interpret=lambda t, c: {"substantive_needs": [{"kind": "merge"}]},
        exception=lambda t, c: {"reviewed": True},
        capacity_fn=lambda: {"io": 4, "low": 2, "exception": 1},
        resource_keys=lambda t, c: ["branch:o/r@shared"],
    )
    closer_pipeline.run_pipeline(
        state, ["o/r#1"], act=lambda t, c: {"status": "uncertain"}, **common
    )
    calls = []
    second = closer_pipeline.run_pipeline(
        state,
        ["o/r#2"],
        act=lambda t, c: calls.append(t) or {"status": "completed", "target_complete": True},
        **common,
    )
    assert not calls
    assert second["scoped"]["unknown_reservations"]["branch:o/r@shared"] == "o/r#1"


def test_save_rechecks_expiry_after_serialization(tmp_path, monkeypatch):
    root = closer_resources.open_root(tmp_path / "s", pipeline_schema=closer_pipeline.SCHEMA)
    clock = [100.0]
    lease = closer_resources.acquire(
        root, "o/r#1", owner="mine", expires_at=101, now=lambda: clock[0]
    )
    original = closer_pipeline.json.dump

    def serialize(*a, **kw):
        value = original(*a, **kw)
        clock[0] = 102
        return value

    monkeypatch.setattr(closer_pipeline.json, "dump", serialize)
    try:
        with closer_resources.fenced(lease, now=lambda: clock[0]):
            with pytest.raises(closer_resources.StaleLease):
                closer_pipeline._save(lease.state_path, closer_pipeline._empty())
        assert not lease.state_path.exists()
    finally:
        lease.release()


def _captured_failed_act_shape(*, barrier):
    # Sanitized shape from the actual current queue: compact failed act after an
    # unsupported return, with no affirmative completed/no-effect receipt.
    row = {
        "status": "failed",
        "stage": "act",
        "results": {},
        "error": "act returned unsupported or missing status",
        "revision": "retained-revision",
        "identity": "retained-identity",
    }
    if barrier:
        row["recovery_barrier"] = {
            "schema": "closer-unverified-act-barrier/v1",
            "source_run_id": "original-run",
            "item_sha256": "a" * 64,
            "act_result_sha256": "b" * 64,
            "revision": row["revision"],
            "identity": row["identity"],
            "work_key": "original-work",
        }
    return row


@pytest.mark.parametrize("barrier", [False, True])
@pytest.mark.parametrize("direction", ["forward", "rollback"])
@pytest.mark.parametrize("location", ["run", "carry"])
def test_mode_switch_retains_captured_failed_act_unknown_and_disjoint_progress(
    tmp_path, barrier, direction, location
):
    state, log = tmp_path / "s.json", tmp_path / "events"
    target, other = "o/r#1", "other/repo#2"
    row = _captured_failed_act_shape(barrier=barrier)
    doc = {"schema": closer_pipeline.SCHEMA, "cache": {}, "carry": {}, "runs": {}}
    if location == "run":
        doc["runs"] = {"original": {"created": 1, "deadline": 2, "targets": {target: row}}}
    else:
        doc["carry"][target] = row
    if direction == "forward":
        retained = state
    else:
        root = closer_resources.open_root(state, pipeline_schema=closer_pipeline.SCHEMA)
        retained = closer_resources.shard_dir(root, target) / "state.json"
        retained.parent.mkdir(parents=True, exist_ok=True)
    retained.write_text(json.dumps(doc))
    before = retained.read_bytes()
    result = finish(
        spawn(
            {
                "state": str(state),
                "targets": [target, other],
                "log": str(log),
                "mode": "resource_scoped" if direction == "forward" else "legacy",
            }
        )
    )
    prefix = "legacy" if direction == "forward" else "scoped"
    assert result["targets"][target]["deferred_reason"] == prefix + "_unverified_act_outcome"
    assert result["targets"][other]["status"] == "completed"
    assert retained.read_bytes() == before
    assert acts(log) == [other]
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert not any(event["t"] == target for event in events)


@pytest.mark.parametrize("stage", ["prepare", "interpret"])
def test_failed_read_is_not_misclassified_as_unknown_act(tmp_path, stage):
    state = tmp_path / "s.json"
    row = _captured_failed_act_shape(barrier=False)
    row["stage"] = stage
    row["error"] = "ordinary read failed before external execution"
    state.write_text(json.dumps({"runs": {"old": {"targets": {"o/r#1": row}}}, "carry": {}}))
    assert closer_resources.legacy_unresolved(state) == ({}, None)


def test_affirmative_native_no_effect_wait_is_preserved_without_discharging_barrier(
    tmp_path, monkeypatch
):
    target = "o/r#1"
    monkeypatch.setattr(closer_pipeline, "_legacy_failed_act_wait_proof", lambda *a: True)
    assert (
        closer_resources._unresolved_effect(
            tmp_path / "s", target, _captured_failed_act_shape(barrier=False)
        )
        is None
    )
    assert (
        closer_resources._unresolved_effect(
            tmp_path / "s", target, _captured_failed_act_shape(barrier=True)
        )
        == "unverified_act_outcome"
    )


@pytest.mark.parametrize("location", ["run", "carry"])
def test_forward_malformed_legacy_row_holds_unknown_inventory(tmp_path, location):
    state, log = tmp_path / "s.json", tmp_path / "events"
    doc = {"runs": {}, "carry": {}}
    if location == "run":
        doc["runs"]["old"] = {"targets": {"o/r#1": None}}
    else:
        doc["carry"]["o/r#1"] = None
    state.write_text(json.dumps(doc))
    before = state.read_bytes()
    found, error = closer_resources.legacy_unresolved(state)
    assert found == {} and error is not None
    assert "not an object" in error
    result = finish(
        spawn(
            {
                "state": str(state),
                "mode": "resource_scoped",
                "targets": ["o/r#1", "other/repo#2"],
                "log": str(log),
            }
        )
    )
    assert {row["deferred_reason"] for row in result["targets"].values()} == {
        "legacy_state_unreadable"
    }
    assert state.read_bytes() == before and not log.exists()


@pytest.mark.parametrize("direction", ["forward", "rollback"])
@pytest.mark.parametrize("location", ["run", "carry"])
def test_read_quarantines_fence_mode_switch_without_blocking_disjoint(
    tmp_path, direction, location
):
    state, log = tmp_path / "s.json", tmp_path / "events"
    rows = {}
    errors = [
        closer_pipeline.READ_BARRIER_ERROR,
        closer_pipeline.READ_INTERRUPTED_BARRIER_ERROR,
        closer_pipeline.READ_ORIGIN_BARRIER_ERROR,
    ]
    for index, error in enumerate(errors):
        target = f"example/repo#{index + 1}"
        rows[target] = {
            "status": "failed",
            "stage": "interpret" if index < 2 else "prepare",
            "error": error,
            "identity": target,
            "revision": "retained-revision",
            "results": {},
            "recovery_barrier": {
                "schema": closer_pipeline.READ_BARRIER_SCHEMA,
                "source_run_id": "retained-original-run",
                "item_sha256": "a" * 64,
                "act_result_sha256": "b" * 64,
                "revision": "retained-revision",
                "identity": target,
                "work_key": "original-work",
                "reason": "unverified_terminal_boundary",
            },
        }
    doc = {
        "schema": closer_pipeline.SCHEMA,
        "cache": {},
        "carry": rows if location == "carry" else {},
        "runs": (
            {"original": {"created": 1, "deadline": 2, "targets": rows}}
            if location == "run"
            else {}
        ),
    }
    if direction == "forward":
        retained = state
    else:
        root = closer_resources.open_root(state, pipeline_schema=closer_pipeline.SCHEMA)
        retained = closer_resources.shard_dir(root, next(iter(rows))) / "state.json"
        retained.parent.mkdir(parents=True, exist_ok=True)
    retained.write_text(json.dumps(doc))
    before = retained.read_bytes()
    other = "other/repo#999"
    result = finish(
        spawn(
            {
                "state": str(state),
                "targets": [*rows, other],
                "log": str(log),
                "mode": "resource_scoped" if direction == "forward" else "legacy",
            }
        )
    )
    prefix = "legacy" if direction == "forward" else "scoped"
    for target in rows:
        assert result["targets"][target]["deferred_reason"] == prefix + "_unverified_read_outcome"
        assert result["targets"][target]["scoped_fence"] is True
    assert result["targets"][other]["status"] == "completed"
    assert retained.read_bytes() == before
    assert acts(log) == [other]
    assert not any(json.loads(line)["t"] in rows for line in log.read_text().splitlines())


@pytest.mark.parametrize("stage", ["prepare", "interpret", "exception", "act", "reconcile"])
@pytest.mark.parametrize(
    "marker", ["read_barrier", "act_barrier", "provider_uncertain", "read_error"]
)
def test_unresolved_read_or_provider_boundary_cannot_disappear_with_stage_change(
    tmp_path, stage, marker
):
    row = {"status": "failed", "stage": stage, "error": "ordinary failure", "results": {}}
    expected = "unverified_read_outcome"
    if marker.endswith("barrier"):
        row["recovery_barrier"] = {
            "schema": (
                closer_pipeline.READ_BARRIER_SCHEMA
                if marker == "read_barrier"
                else "closer-unverified-act-barrier/v1"
            )
        }
        if marker == "act_barrier":
            expected = "unverified_act_outcome"
    elif marker == "provider_uncertain":
        row["provider_outcome_uncertain"] = {"actual": {"run_id": "retained-provider-run"}}
        expected = "provider_uncertain_outcome"
    else:
        row["error"] = closer_pipeline.READ_INTERRUPTED_BARRIER_ERROR
    assert closer_resources._unresolved_effect(tmp_path / "state", "o/r#1", row) == expected


@pytest.mark.parametrize("continuation", ["pending", "progress"])
def test_own_intents_release_only_after_durable_known_continuation(tmp_path, continuation):
    state = tmp_path / "state.json"
    target = "example/repo#1"
    actions = []

    def act(name, context):
        actions.append(name)
        if len(actions) == 1:
            if continuation == "pending":
                return {
                    "status": "pending",
                    "pending_kind": "external_event",
                    "next_trigger": "event_changed",
                    "pending_evidence": {"event_id": "native-event"},
                }
            return {"status": "completed", "progress_key": "exact-known-step"}
        return {"status": "completed", "target_complete": True}

    kwargs = dict(
        coordinator_mode="resource_scoped",
        prepare=lambda *_: {"revision": "rev", "identity": target},
        interpret=lambda *_: {"substantive_needs": []},
        exception=lambda *_: pytest.fail("exception must not run"),
        act=act,
        capacity_fn=lambda: {"io": 2, "low": 1, "exception": 1},
        resource_keys=lambda *_: ["branch:example/repo@feature"],
    )
    first = closer_pipeline.run_pipeline(state, [target], deadline=time.time() + 20, **kwargs)
    root = closer_resources.scoped_root(state)
    assert closer_resources.unknown_reservations(root) == {}
    if continuation == "pending":
        assert first["targets"][target]["status"] == "pending"
        second = closer_pipeline.run_pipeline(state, [target], deadline=time.time() + 20, **kwargs)
        assert second["targets"][target]["status"] == "completed"
    else:
        assert first["targets"][target]["status"] == "completed"
    assert actions == [target, target]
    assert closer_resources.unknown_reservations(root) == {}


@pytest.mark.parametrize(
    "bad",
    [
        "unverified_pending",
        "unverified_deferred",
        "read_barrier",
        "act_barrier",
        "provider_uncertain",
        "exception",
        "unknown_act",
        "wrong_run",
    ],
)
def test_continuation_release_retains_unknown_or_quarantined_intents(tmp_path, bad):
    state = tmp_path / "state.json"
    target = "example/repo#1"
    root = closer_resources.open_root(state, pipeline_schema=closer_pipeline.SCHEMA)
    with closer_resources.reserve(
        root, ["pr:" + target], target=target, owner="owner", deadline=9e9
    ):
        pass
    row = {
        "status": "pending",
        "stage": "act",
        "pending_kind": "external_event",
        "deferred_next_trigger": "event_changed",
        "pending_evidence": {"event_id": "native-event"},
        "results": {},
    }
    if bad == "unverified_pending":
        row["pending_evidence"] = {}
    elif bad == "unverified_deferred":
        row.update(status="deferred", deferred_reason="worker_capacity", resource_evidence={})
    elif bad.endswith("barrier"):
        row["recovery_barrier"] = {
            "schema": "closer-unverified-"
            + ("read" if bad.startswith("read") else "act")
            + "-barrier/v1"
        }
    elif bad == "provider_uncertain":
        row["provider_outcome_uncertain"] = {"actual": {"run_id": "unresolved"}}
    elif bad == "exception":
        row["stage"] = "exception"
    elif bad == "unknown_act":
        row["results"]["act"] = {"status": "unknown"}
    doc = closer_pipeline._empty()
    doc["runs"] = {
        "run": {
            "created": time.time(),
            "deadline": time.time() + 20,
            "assignment": [target],
            "targets": {target: row},
        }
    }
    closer_pipeline._save(state, doc)
    assert not closer_resources._release_known_shard_boundary(
        root,
        state,
        target,
        owner="owner",
        now=time.time,
        run_id="other" if bad == "wrong_run" else "run",
    )
    assert closer_resources.unknown_reservations(root) == {"pr:" + target: target}


def test_known_continuation_never_releases_foreign_owner_or_hides_other_unknown(tmp_path):
    state = tmp_path / "state.json"
    target = "example/repo#1"
    root = closer_resources.open_root(state, pipeline_schema=closer_pipeline.SCHEMA)
    with closer_resources.reserve(
        root, ["pr:" + target], target=target, owner="foreign", deadline=9e9
    ):
        pass
    row = {
        "status": "pending",
        "stage": "act",
        "pending_kind": "external_event",
        "deferred_next_trigger": "event_changed",
        "pending_evidence": {"event_id": "native-event"},
        "results": {},
    }
    doc = closer_pipeline._empty()
    doc["runs"] = {
        "run": {
            "created": time.time(),
            "deadline": time.time() + 20,
            "assignment": [target],
            "targets": {target: row},
        }
    }
    closer_pipeline._save(state, doc)
    closer_resources._release_known_shard_boundary(
        root, state, target, owner="new-owner", now=time.time, run_id="run"
    )
    assert closer_resources.unknown_reservations(root) == {"pr:" + target: target}
    assert (
        "unresolved_resource_reservations"
        in closer_pipeline.validate_stop(state, [target])["reasons"]
    )


def test_strict_native_no_operation_wait_releases_current_intent(tmp_path):
    import closer_queue

    state = tmp_path / "state.json"
    target = "example/repo#1"
    head = "a" * 40
    trigger = "source owner supplies current-head proof or resolves the named finding"
    calls = []

    def act(name, context):
        calls.append(name)
        receipt = closer_queue.run_round(
            context["action_state_path"],
            [target],
            lambda _: {
                "target": target,
                "revision": "rev",
                "action": "wait",
                "eligible": False,
                "requires_coordinator": False,
                "reason": "worker_json_invalid",
                "next_trigger": trigger,
                "evidence": {"head": head},
            },
            lambda *_: pytest.fail("native wait must not mutate"),
            deadline=time.time() + 20,
            max_workers=1,
            max_passes=1,
        )
        proof = {
            "schema": closer_pipeline.INNER_WAIT_SCHEMA,
            "target": target,
            "revision": "rev",
            "head": head,
            "reason": "worker_json_invalid",
            "next_trigger": trigger,
            "inner_path": context["action_state_path"],
            "receipt_sha256": closer_pipeline._digest(receipt),
        }
        return {
            "status": "deferred",
            "reason": "validated_inner_wait",
            "next_trigger": trigger,
            "resource_evidence": {"inner_wait": proof},
            "queue_receipt": receipt,
        }

    kwargs = dict(
        coordinator_mode="resource_scoped",
        prepare=lambda *_: {
            "revision": "rev",
            "identity": target,
            "snapshot": {"target": target, "revision": "rev", "head": head, "kind": "pr"},
        },
        interpret=lambda *_: {"substantive_needs": []},
        exception=lambda *_: pytest.fail("no exception"),
        act=act,
        capacity_fn=lambda: {"io": 2, "low": 1, "exception": 1},
        resource_keys=lambda *_: ["branch:example/repo@feature"],
    )
    for _ in range(2):
        receipt = closer_pipeline.run_pipeline(state, [target], deadline=time.time() + 20, **kwargs)
        assert receipt["targets"][target]["deferred_reason"] == "validated_inner_wait"
        assert closer_pipeline.validate_stop_scoped(state, receipt["round_id"], [target])["valid"]
        assert closer_resources.unknown_reservations(closer_resources.scoped_root(state)) == {}
    assert calls == [target, target]
    shard = closer_resources.shard_dir(closer_resources.scoped_root(state), target) / "state.json"
    native = shard.parent / f"{shard.stem}.actions" / f"{closer_pipeline._digest(target)}.json"
    original_native = native.read_bytes()
    changed = closer_queue._read_state(native)
    changed["items"][target]["operation"] = {"id": "unresolved", "status": "uncertain"}
    closer_queue._atomic_write(native, changed)
    verdict = closer_pipeline.validate_stop_scoped(state, receipt["round_id"], [target])
    assert not verdict["valid"]
    assert any("unverified_inner_wait" in reason for reason in verdict["reasons"])
    native.write_bytes(original_native)
    assert closer_pipeline.validate_stop_scoped(state, receipt["round_id"], [target])["valid"]


@pytest.mark.parametrize(
    "tamper", ["act_progress", "known_completion", "retained_error", "empty_uncertainty"]
)
def test_progress_release_requires_exact_durable_completion_binding(tmp_path, tamper):
    state = tmp_path / "state.json"
    target = "example/repo#1"
    root = closer_resources.open_root(state, pipeline_schema=closer_pipeline.SCHEMA)
    with closer_resources.reserve(
        root, ["pr:" + target], target=target, owner="owner", deadline=9e9
    ):
        pass
    row = {
        "status": "running",
        "stage": "prepare",
        "known_completed_action": True,
        "progress_key": "exact-step",
        "results": {"act": {"status": "completed", "progress_key": "exact-step"}},
    }
    if tamper == "act_progress":
        row["results"]["act"]["progress_key"] = "different-step"
    elif tamper == "known_completion":
        row["known_completed_action"] = False
    elif tamper == "retained_error":
        row["error"] = "act returned unsupported or missing status"
    else:
        row["provider_outcome_uncertain"] = {}
    doc = closer_pipeline._empty()
    doc["runs"] = {
        "run": {
            "created": time.time(),
            "deadline": time.time() + 20,
            "assignment": [target],
            "targets": {target: row},
        }
    }
    closer_pipeline._save(state, doc)
    assert not closer_resources._release_known_shard_boundary(
        root, state, target, owner="owner", now=time.time, progress=True
    )
    assert closer_resources.unknown_reservations(root) == {"pr:" + target: target}


def test_named_stop_selects_exact_original_run_and_never_substitutes_latest(tmp_path):
    state = tmp_path / "state.json"
    target = "example/repo#1"
    old = {
        "created": 1,
        "deadline": 2,
        "assignment": [target],
        "targets": {
            target: {
                "status": "pending",
                "stage": "act",
                "pending_kind": "external_event",
                "deferred_next_trigger": "fresh_event",
                "pending_evidence": {"event_id": "recorded-native-event"},
            }
        },
    }
    doc = closer_pipeline._empty()
    doc["runs"] = {
        "recorded": old,
        "newer": {
            "created": 3,
            "deadline": 4,
            "assignment": [target],
            "targets": {target: {"status": "completed"}},
        },
    }
    closer_pipeline._save(state, doc)
    before = state.read_bytes()
    original = closer_pipeline._validate_stop_legacy(state, [target], expected_run_id="recorded")
    assert (
        original["valid"] and original["run_id"] == "recorded" and original["continuation_required"]
    )
    assert closer_pipeline._validate_stop_legacy(state, [target])["run_id"] == "newer"
    assert closer_pipeline._validate_stop_legacy(state, [target], expected_run_id="missing")[
        "reasons"
    ] == ["no_matching_pipeline_run"]
    old["targets"][target]["pending_evidence"] = {}
    closer_pipeline._save(state, doc)
    verdict = closer_pipeline._validate_stop_legacy(state, [target], expected_run_id="recorded")
    assert not verdict["valid"] and verdict["run_id"] == "recorded"
    assert "unverifiable_pending_work:" + target in verdict["reasons"]
    assert before != state.read_bytes()
