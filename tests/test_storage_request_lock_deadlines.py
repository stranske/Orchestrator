"""Real contended local locks defer reads without disturbing a holder or ledger."""

import contextlib
import fcntl
import json
import os
import threading
import time

import pytest
from test_closer_verifier_attribution import world as world
from test_storage_lifecycle import job as job

import closer_attribution_store as attribution
import storage_lifecycle as storage


@contextlib.contextmanager
def held(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    # A broken blocking implementation still returns, so this regression cannot hang.
    timer = threading.Timer(0.6, lambda: fcntl.flock(fd, fcntl.LOCK_UN))
    timer.start()
    try:
        yield fd
    finally:
        timer.cancel()
        timer.join()
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def manager(tmp_path, deadline=None):
    return storage.Reservations(
        tmp_path / "ledger", tmp_path, floor=0, free=lambda _: 10**9, deadline=deadline
    )


def test_real_reservation_contention_is_typed_no_effect_and_preserves_holder(tmp_path):
    m = manager(tmp_path, time.monotonic() + 0.06)
    before = {"jobs": {"foreign": {"remaining": 20, "status": "uncertain"}}, "peaks": {"old": 70}}
    storage.write_json(m.directory / "reservations.json", before)
    with held(m.directory / "lock"):
        with pytest.raises(storage.StorageLockWait) as caught:
            m.admit("new", "request", 0, 30, owner_token="test")
        assert caught.value.accounting == {
            "status": "storage_lock_wait",
            "scope": "reservation_ledger",
            "reason": "request_deadline_exhausted",
        }
        assert json.loads((m.directory / "reservations.json").read_text()) == before
        contender = os.open(m.directory / "lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(contender)


def test_real_attribution_index_wait_has_no_published_object(tmp_path):
    m = manager(tmp_path)
    store = attribution.Store(tmp_path / "cache", ledger=m, deadline=time.monotonic() + 0.06)
    with held(store.control / "lock"):
        with pytest.raises(attribution.AttributionStorageWait) as caught:
            store.retain(b"authentic", "txt")
        assert caught.value.accounting["scope"] == "attribution_index"
        assert not (store.control / "index.json").exists()
        assert not (store.root / "raw").exists()
        assert not (m.directory / "reservations.json").exists()


def test_nested_index_then_foreign_ledger_contention_uses_same_request_deadline(tmp_path):
    m = manager(tmp_path)
    before = {"jobs": {"foreign": {"remaining": 123, "status": "uncertain"}}, "peaks": {}}
    storage.write_json(m.directory / "reservations.json", before)
    store = attribution.Store(tmp_path / "cache", ledger=m, deadline=time.monotonic() + 0.06)
    with held(m.directory / "lock"):
        with pytest.raises(attribution.AttributionStorageWait) as caught:
            store.retain(b"input", "txt")
        assert caught.value.accounting["scope"] == "reservation_ledger"
        assert json.loads((m.directory / "reservations.json").read_text()) == before
        assert not (store.control / "index.json").exists()
        assert not (store.root / "raw").exists()
    # The store index lock unwound too, without deleting either lock file.
    fd = os.open(store.control / "lock", os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.close(fd)


def test_expired_deadline_refuses_even_uncontended_without_mutation(tmp_path):
    m = manager(tmp_path)
    with pytest.raises(storage.StorageLockWait):
        m.admit("no", "request", 0, 1, owner_token="test", deadline=time.monotonic() - 1)
    assert not (m.directory / "reservations.json").exists()


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf")])
def test_invalid_deadline_is_not_an_unbounded_wait(tmp_path, bad):
    m = manager(tmp_path)
    with pytest.raises(ValueError, match="finite monotonic"):
        with m.locked(deadline=bad):
            pytest.fail("must not enter critical section")


def test_default_calls_remain_compatible_and_finish_with_explicit_deadline(tmp_path):
    m = manager(tmp_path)
    row = m.admit("one", "request", 0, 10, owner_token="test")
    assert row["remaining"] == 10
    m.finish("one", {"status": "retained"}, terminal=True, deadline=time.monotonic() + 1)
    assert json.loads((m.directory / "reservations.json").read_text())["jobs"] == {}


def test_owner_guard_wait_is_prechild_and_releases_only_own_reservation(job, monkeypatch):
    monkeypatch.setattr(storage, "identity", lambda command: "stable-source")
    monkeypatch.setattr(storage, "staging_bytes", lambda *a, **k: 0)
    monkeypatch.setattr(storage, "_qualified_cold_plan", lambda *a, **k: None)
    monkeypatch.setattr(
        storage.subprocess, "Popen", lambda *a, **k: pytest.fail("no child before owner lock")
    )
    acquire = storage.acquire_exclusive
    owner_fd = []

    def contended(fd, *, deadline=None, scope="storage"):
        if scope == "storage_job_owner":
            owner_fd.append(fd)
            raise storage.StorageLockWait(scope)
        return acquire(fd, deadline=deadline, scope=scope)

    monkeypatch.setattr(storage, "acquire_exclusive", contended)
    with pytest.raises(storage.StorageLockWait) as caught:
        storage.run_job(job, free=lambda _: 100 * storage.GIB, lock_deadline=time.monotonic() + 1)
    assert caught.value.accounting["scope"] == "storage_job_owner"
    assert json.loads((job.state_dir / "reservations.json").read_text())["jobs"] == {}
    with pytest.raises(OSError):
        os.fstat(owner_fd[0])
    assert not list(job.temp_parent.glob("storage-verify-*"))


def test_finish_lock_wait_retains_charge_and_is_typed_not_remote_unknown(tmp_path):
    m = manager(tmp_path)
    store = attribution.Store(tmp_path / "cache", ledger=m)
    with pytest.raises(attribution.AttributionStorageWait) as caught:
        with store.admitted(100):
            store.deadline = time.monotonic() + 0.06
            with held(m.directory / "lock"):
                store._finish(
                    next(iter(json.loads((m.directory / "reservations.json").read_text())["jobs"])),
                    {"status": "retained"},
                    terminal=True,
                )
    assert caught.value.accounting["scope"] == "reservation_ledger"
    jobs = json.loads((m.directory / "reservations.json").read_text())["jobs"]
    assert len(jobs) == 1
    assert next(iter(jobs.values()))["remaining"] == 100


def test_production_prewrite_contended_store_is_valid_pending_without_post(world):
    from test_closer_verifier_attribution import TARGET, validate

    store = attribution.Store(world.backend.verifier_archive_root)
    world.backend.gh.deadline = time.time() + 0.08
    with held(store.control / "lock"):
        result = world.backend.initiate_verify(TARGET, world.snapshot)
    assert result["status"] == "resource_wait"
    assert result["verifier_resource_wait"]["cause"] == "storage_capacity"
    assert "storage_lock_wait" in result["verifier_resource_wait"]["requirements"][0]
    assert not world.posts and not world.downloads
    assert validate(result, world.snapshot)["status"] == "resource_wait"
