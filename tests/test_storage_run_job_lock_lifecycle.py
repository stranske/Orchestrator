"""A child lifetime does not consume later bounded lifecycle lock budgets."""

import json
import time

import pytest
from test_storage_lifecycle import job as job
from test_storage_request_lock_deadlines import held

import storage_lifecycle as storage


def ledger(job):
    return json.loads((job.state_dir / "reservations.json").read_text())


def test_child_outlives_acquisition_deadline_and_still_retires(job):
    script = job.source / "verify.sh"
    script.write_text(script.read_text().replace('exit "$1"', 'sleep .35\nexit "$1"'))
    assert (
        storage.run_job(
            job,
            free=lambda _: storage.GIB,
            probe=lambda _: None,
            lock_deadline=time.monotonic() + 0.2,
        )
        == 0
    )
    receipt = json.loads(next((job.state_dir / "receipts").glob("*.json")).read_text())
    assert receipt["status"] == "retired"
    assert receipt["exit_code"] == 0
    assert ledger(job)["jobs"] == {}
    assert not list(job.temp_parent.glob("storage-verify-*"))


def seed_foreign(job):
    manager = storage.Reservations(
        job.state_dir, job.temp_parent, floor=0, free=lambda _: storage.GIB
    )
    manager.admit("foreign", "other", 0, 123, owner_token="foreign")
    return ledger(job)["jobs"]["foreign"]


def test_owner_refusal_after_deadline_uses_fresh_finish_budget(job, monkeypatch):
    foreign = seed_foreign(job)
    monkeypatch.setattr(storage, "identity", lambda _: "original")
    original = storage.acquire_exclusive

    def owner_refusal(fd, *, deadline=None, scope="storage"):
        if scope == "storage_job_owner":
            time.sleep(0.08)
            raise storage.StorageLockWait(scope)
        return original(fd, deadline=deadline, scope=scope)

    monkeypatch.setattr(storage, "acquire_exclusive", owner_refusal)
    monkeypatch.setattr(storage.subprocess, "Popen", lambda *a, **k: pytest.fail("no spawn"))
    with pytest.raises(storage.StorageLockWait) as caught:
        storage.run_job(
            job,
            free=lambda _: storage.GIB,
            lock_deadline=time.monotonic() + 0.06,
            lifecycle_lock_timeout_seconds=0.04,
        )
    assert caught.value.accounting["scope"] == "storage_job_owner"
    assert ledger(job)["jobs"] == {"foreign": foreign}
    assert not list(job.temp_parent.glob("storage-verify-*"))


def test_pre_spawn_failure_after_initial_deadline_retires_and_finishes(job, monkeypatch):
    foreign = seed_foreign(job)
    monkeypatch.setattr(storage, "identity", lambda _: "original")

    def refused_bind(*args, **metadata):
        assert "deadline" not in metadata
        time.sleep(0.08)
        raise PermissionError("metadata refused before spawn")

    monkeypatch.setattr(storage.Reservations, "bind", refused_bind)
    monkeypatch.setattr(storage.subprocess, "Popen", lambda *a, **k: pytest.fail("no spawn"))
    with pytest.raises(PermissionError, match="metadata refused before spawn"):
        storage.run_job(
            job,
            free=lambda _: storage.GIB,
            probe=lambda _: None,
            lock_deadline=time.monotonic() + 0.06,
            lifecycle_lock_timeout_seconds=0.04,
        )
    assert ledger(job)["jobs"] == {"foreign": foreign}
    assert not list(job.temp_parent.glob("storage-verify-*"))
    receipts = [json.loads(p.read_text()) for p in (job.state_dir / "receipts").glob("*.json")]
    assert any(r.get("status") == "not_started_retired" for r in receipts)


def test_uncertain_constructor_after_initial_deadline_keeps_own_and_foreign_charge(
    job, monkeypatch
):
    foreign = seed_foreign(job)
    monkeypatch.setattr(storage, "identity", lambda _: "original")

    def uncertain(*args, **kwargs):
        time.sleep(0.08)
        raise OSError("constructor outcome uncertain")

    monkeypatch.setattr(storage.subprocess, "Popen", uncertain)
    with pytest.raises(OSError, match="constructor outcome uncertain"):
        storage.run_job(
            job,
            free=lambda _: storage.GIB,
            probe=lambda _: pytest.fail("retain"),
            lock_deadline=time.monotonic() + 0.06,
            lifecycle_lock_timeout_seconds=0.04,
        )
    jobs = ledger(job)["jobs"]
    assert jobs["foreign"] == foreign and len(jobs) == 2
    receipt = json.loads(next((job.state_dir / "receipts").glob("*.json")).read_text())
    assert receipt["status"] == "retained_uncertain" and receipt["spawn_attempted"] is True
    assert list(job.temp_parent.glob("storage-verify-*"))


def test_real_cleanup_contention_stays_bounded_without_releasing_foreign_or_own(job):
    import contextlib

    foreign = seed_foreign(job)
    with contextlib.ExitStack() as holders:

        def busy_after_child(_root):
            holders.enter_context(held(job.state_dir / "lock"))

        with pytest.raises(storage.StorageLockWait):
            storage.run_job(
                job,
                free=lambda _: storage.GIB,
                probe=busy_after_child,
                lock_deadline=time.monotonic() + 0.2,
                lifecycle_lock_timeout_seconds=0.03,
            )
        jobs = ledger(job)["jobs"]
        assert jobs["foreign"] == foreign and len(jobs) == 2
        assert list(job.temp_parent.glob("storage-verify-*"))


@pytest.mark.parametrize("budget", [True, 0, -1, float("nan"), float("inf")])
def test_invalid_lifecycle_budget_never_admits(job, budget):
    with pytest.raises(ValueError, match="finite positive lifecycle lock timeout"):
        storage.run_job(job, lifecycle_lock_timeout_seconds=budget)
    assert not (job.state_dir / "reservations.json").exists()
