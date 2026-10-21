"""Storage admission and terminal retirement use tiny private fixtures, never live cleanup."""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import storage_lifecycle as storage


def ledger(tmp_path, free=80, floor=40):
    return storage.Reservations(tmp_path / "control", tmp_path, floor=floor, free=lambda _: free)


def test_admission_reserves_staging_peak_and_other_jobs_above_floor(tmp_path):
    manager = ledger(tmp_path)
    manager.admit("one", "verify", 10, 25, owner_token="owner")
    with pytest.raises(storage.StorageBlocked, match='"required_bytes": 85'):
        manager.admit("two", "verify", 10, 25, owner_token="other")
    assert list(json.loads((manager.directory / "reservations.json").read_text())["jobs"]) == [
        "one"
    ]


def _contender(directory, barrier, results, capacity, scope):
    manager = storage.Reservations(
        Path(directory), Path(directory).parent, floor=40, free=lambda _: capacity
    )
    barrier.wait()
    try:
        manager.admit(str(os.getpid()), "verify", 10, 25, owner_token="owner", scope=scope)
        results.put("admitted")
    except storage.StorageBlocked:
        results.put("blocked")


@pytest.mark.parametrize("capacity,scope", [(80, None), (1000, "same-release")])
def test_concurrent_processes_cannot_duplicate_bytes_or_publication_scope(
    tmp_path, capacity, scope
):
    context = multiprocessing.get_context("fork")
    barrier, results = context.Barrier(2), context.Queue()
    processes = [
        context.Process(
            target=_contender, args=(str(tmp_path / "control"), barrier, results, capacity, scope)
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    assert sorted(results.get(timeout=2) for _ in processes) == ["admitted", "blocked"]


def test_staged_charge_requires_exact_owner_and_releases_only_copy_forecast(tmp_path):
    manager = ledger(tmp_path)
    manager.admit("one", "verify", 10, 25, owner_token="owner")
    with pytest.raises(storage.StorageBlocked, match="owner token"):
        manager.staged("one", "successor")
    manager.staged("one", "owner")
    data = json.loads((manager.directory / "reservations.json").read_text())
    assert data["jobs"]["one"]["remaining"] == 25
    manager.admit("two", "verify", 10, 25, owner_token="other")


def test_peak_learning_never_lowers_forecast_and_completed_job_cannot_replay(tmp_path):
    manager = ledger(tmp_path, free=1000)
    manager.admit("one", "verify", 10, 25, owner_token="owner")
    manager.finish("one", {"observed_growth_bytes": 90}, terminal=True)
    row = manager.admit("two", "verify", 10, 1, owner_token="owner")
    assert row["peak"] == 90
    manager.finish("two", {"observed_growth_bytes": 2}, terminal=True)
    with pytest.raises(storage.StorageBlocked, match="existing job"):
        manager.admit("one", "verify", 0, 0, owner_token="owner")
    assert manager.admit("three", "verify", 0, 0, owner_token="owner")["peak"] == 90


def test_uncertain_reservations_have_no_time_expiry(tmp_path, monkeypatch):
    manager = ledger(tmp_path)
    manager.admit("one", "verify", 10, 25, owner_token="owner")
    manager.finish("one", {"error": "interrupted"}, terminal=False)
    monkeypatch.setattr(storage.time, "time", lambda: 10**30)
    with pytest.raises(storage.StorageBlocked):
        manager.admit("two", "verify", 10, 25, owner_token="owner")


def test_sparse_and_hardlinked_allocation_is_not_promised_recovery(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    path = source / "sparse"
    with path.open("wb") as stream:
        stream.seek(1024**2)
        stream.write(b"x")
    (source / "hardlink").hardlink_to(path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside" * 100)
    (source / "link").symlink_to(outside)
    real_stat = Path.lstat

    def shared_extents(entry):
        value = real_stat(entry)
        if entry.name in ("sparse", "hardlink"):
            return SimpleNamespace(
                st_dev=value.st_dev,
                st_ino=value.st_ino,
                st_blocks=1,
                st_size=value.st_size,
                st_mode=value.st_mode,
            )
        return value

    # APFS/sparse extent allocation is filesystem dependent; simulate shared
    # extents instead of assuming that seek() creates holes on this filesystem.
    monkeypatch.setattr(Path, "lstat", shared_extents)
    measured = storage.footprint(source)
    assert measured["logical"] == 1024**2 + 1
    assert measured["allocated"] < measured["logical"]
    assert storage.footprint(source, stop_after=100)["over_limit"] is True


def test_retirement_credit_comes_only_from_actual_free_byte_measurement(tmp_path):
    manager = ledger(tmp_path, free=1000)
    manager.admit("one", "verify", 10, 25, owner_token="owner")
    manager.finish("one", {"status": "retired", "catalog_allocated_bytes": 10**12}, terminal=True)
    saved = json.loads((manager.directory / "receipts" / "one.json").read_text())
    assert saved["available_after"] == 1000


def test_staging_estimate_omits_oversize_state_and_own_control_metadata(tmp_path):
    source, runtime = tmp_path / "source", tmp_path / "runtime"
    source.mkdir()
    runtime.mkdir()
    (source / "module.py").write_text("pass\n")
    before = storage.staging_bytes(source, runtime, runtime, 100)
    control = runtime / ".storage-lifecycle"
    control.mkdir()
    (control / "old-receipt").write_bytes(b"x" * 1000)
    large = runtime / "large"
    large.mkdir()
    (large / "payload").write_bytes(b"x" * 1000)
    assert storage.staging_bytes(source, runtime, runtime, 100) == before


def test_unknown_state_copy_symlink_does_not_underestimate(tmp_path):
    source, runtime = tmp_path / "source", tmp_path / "runtime"
    source.mkdir()
    runtime.mkdir()
    (runtime / "alias").symlink_to(source)
    with pytest.raises(storage.StorageBlocked, match="symlink"):
        storage.staging_bytes(source, runtime, runtime, 100)


def test_nested_custom_control_cannot_recursively_enter_a_state_copy(tmp_path):
    source, runtime = tmp_path / "source", tmp_path / "runtime"
    source.mkdir()
    control = runtime / "data/control"
    control.mkdir(parents=True)
    with pytest.raises(storage.StorageBlocked, match="control nested"):
        storage.staging_bytes(source, runtime, runtime, 2**20, exclude=control)


@pytest.fixture
def job(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "identity").write_text("original")
    script = source / "verify.sh"
    script.write_text("""#!/bin/bash
if [[ "$1" == "--identity" ]]; then cat "$2/identity"; exit 0; fi
mkdir -p "$TMPDIR/pytest-of-owner" "$VERIFY_BEFORE_SYNC_TMP_PARENT/verify-before-sync.fake"
mkdir -p "$VERIFY_BEFORE_SYNC_TMP_PARENT/verify-before-sync.fake/state"
printf unique-failure-state > "$VERIFY_BEFORE_SYNC_TMP_PARENT/verify-before-sync.fake/state/input"
printf disposable > "$TMPDIR/pytest-of-owner/fixture"
printf diagnostic > "$VERIFY_BEFORE_SYNC_TMP_PARENT/verify-before-sync.fake/verify.log"
[[ -z "${USE_REAL_STAGE_HELPER:-}" ]] || "$PYTHON_BIN" "$USE_REAL_STAGE_HELPER" staged --state-dir "$ORCH_STORAGE_STATE_DIR" --job "$ORCH_STORAGE_RUN_ID" || exit 9
[[ -z "${ENTRY_OBSERVATION:-}" ]] || printf '%s:%s' "$$" "$ORCH_STORAGE_ENTRY_PID" > "$ENTRY_OBSERVATION"
[[ "${CHANGE_SOURCE:-0}" != 1 ]] || printf changed > "${SOURCE_IDENTITY}"
exit "$1"
""")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    return argparse.Namespace(
        state_dir=tmp_path / "control",
        source=source,
        runtime=runtime,
        state_source=runtime,
        temp_parent=tmp_path,
        max_dir_mb=1,
        peak_bytes=32,
        profile="fixture",
        local_strict=False,
        command=["bash", str(script), "0"],
    )


def receipt(job):
    files = list((job.state_dir / "receipts").glob("*.json"))
    assert len(files) == 1
    return json.loads(files[0].read_text())


@pytest.mark.parametrize("exit_code", [0, 7])
def test_terminal_success_and_failure_retire_temp_and_keep_compact_evidence(job, exit_code):
    job.command[-1] = str(exit_code)
    assert storage.run_job(job, free=lambda _: storage.GIB, probe=lambda _: None) == exit_code
    result = receipt(job)
    assert result["exit_code"] == exit_code and result["diagnostic_tail"] == "diagnostic"
    assert result["source_identity"] == "original" and result["source_unchanged"] is True
    if exit_code == 0:
        assert result["status"] == "retired" and not Path(result["root"]).exists()
    else:
        assert result["status"] == "fixtures_retired_failure_retained"
        root = Path(result["root"])
        assert (
            root / "staging/verify-before-sync.fake/state/input"
        ).read_text() == "unique-failure-state"
        assert not (root / "tmp").exists()
    assert not Path(result["retirement_path"]).exists()
    assert json.loads((job.state_dir / "reservations.json").read_text())["jobs"] == {}
    assert not list(job.state_dir.rglob("*.tar*"))


def test_changed_source_is_retained_without_holding_future_growth(job, monkeypatch):
    monkeypatch.setenv("CHANGE_SOURCE", "1")
    monkeypatch.setenv("SOURCE_IDENTITY", str(job.source / "identity"))
    assert storage.run_job(job, free=lambda _: storage.GIB, probe=lambda _: None) == 0
    result = receipt(job)
    assert result["status"] == "retained_source_changed" and Path(result["root"]).is_dir()
    assert json.loads((job.state_dir / "reservations.json").read_text())["jobs"] == {}


def test_explicit_keep_preserves_owned_tree(job, monkeypatch):
    monkeypatch.setenv("VERIFY_BEFORE_SYNC_KEEP", "1")
    assert storage.run_job(job, free=lambda _: storage.GIB, probe=lambda _: None) == 0
    result = receipt(job)
    assert result["status"] == "retained_requested" and Path(result["root"]).is_dir()


def test_reentry_marker_is_bound_to_child_pid_not_inherited_parent(job, monkeypatch):
    observation = job.temp_parent / "entry"
    monkeypatch.setenv("ORCH_STORAGE_ENTRY_PID", "stale-parent")
    monkeypatch.setenv("ENTRY_OBSERVATION", str(observation))
    assert storage.run_job(job, free=lambda _: storage.GIB, probe=lambda _: None) == 0
    left, right = observation.read_text().split(":")
    assert left == right and left != "stale-parent"


def test_real_child_stage_cli_updates_only_its_authenticated_reservation(job, monkeypatch):
    monkeypatch.setenv("USE_REAL_STAGE_HELPER", storage.__file__)
    monkeypatch.setenv("PYTHON_BIN", sys.executable)
    assert storage.run_job(job, free=lambda _: storage.GIB, probe=lambda _: None) == 0
    result = receipt(job)
    assert result["remaining"] == result["peak"]


@pytest.mark.parametrize("error", [PermissionError, KeyboardInterrupt])
def test_failed_or_interrupted_cleanup_preserves_remnant_and_does_not_retry(
    job, monkeypatch, error
):
    calls = []

    def fail(path, **_kwargs):
        calls.append(path)
        raise error("cleanup refused/interrupted")

    fail.avoids_symlink_attacks = True
    monkeypatch.setattr(storage.shutil, "rmtree", fail)
    with pytest.raises(error):
        storage.run_job(job, free=lambda _: storage.GIB, probe=lambda _: None)
    result = receipt(job)
    assert len(calls) == 1
    assert Path(result["retirement_path"]).is_dir()
    assert result["status"] == "cleanup_pending" and result["diagnostic_tail"] == "diagnostic"
    # No writer remains: resident bytes count through actual free space, not a
    # fictitious future allocation or a cleanup-credit estimate.
    assert json.loads((job.state_dir / "reservations.json").read_text())["jobs"] == {}


def test_inspection_denial_preserves_tree_and_reservation(job):
    calls = []

    def denied(root):
        calls.append(root)
        raise PermissionError("inspection denied")

    with pytest.raises(PermissionError, match="inspection denied"):
        storage.run_job(job, free=lambda _: storage.GIB, probe=denied)
    result = receipt(job)
    assert len(calls) == 1 and Path(result["root"]).is_dir()
    assert result["status"] == "retained_uncertain"
    assert json.loads((job.state_dir / "reservations.json").read_text())["jobs"]


def test_interrupted_supervisor_never_retires_or_evacuates_live_writer(job, monkeypatch):
    class Running:
        pid = 987654321

        def poll(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(storage, "identity", lambda _: "original")
    monkeypatch.setattr(storage.subprocess, "Popen", lambda *_args, **_kw: Running())
    with pytest.raises(KeyboardInterrupt):
        storage.run_job(
            job, free=lambda _: storage.GIB, probe=lambda _: pytest.fail("must not retire")
        )
    result = receipt(job)
    assert result["child_pgid"] == Running.pid and Path(result["root"]).is_dir()
    assert result["status"] == "retained_uncertain"
    assert json.loads((job.state_dir / "reservations.json").read_text())["jobs"]


@pytest.mark.parametrize("replacement", ["directory", "symlink", "token"])
def test_successor_or_alias_cannot_be_retired(tmp_path, replacement):
    root, outside = tmp_path / "owned", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "user").write_text("preserve")
    storage.write_json(root / "owner.json", {"token": "original"})
    original = root.stat()
    if replacement == "token":
        storage.write_json(root / "owner.json", {"token": "successor"})
    else:
        root.rename(tmp_path / "original")
        if replacement == "symlink":
            root.symlink_to(outside, target_is_directory=True)
        else:
            root.mkdir()
            (root / "successor").write_text("preserve")
    with pytest.raises(storage.StorageBlocked):
        storage.retire(root, (original.st_dev, original.st_ino), "original", lambda _: None)
    assert (outside / "user").read_text() == "preserve" and root.exists()


def test_owned_retirement_unlinks_aliases_without_traversing_user_data(tmp_path):
    root, outside = tmp_path / "owned", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "user").write_text("preserve")
    (root / "alias").symlink_to(outside, target_is_directory=True)
    (root / "hardlink").hardlink_to(outside / "user")
    storage.write_json(root / "owner.json", {"token": "owner"})
    value = root.stat()
    storage.retire(root, (value.st_dev, value.st_ino), "owner", lambda _: None)
    assert not root.exists() and (outside / "user").read_text() == "preserve"


@pytest.mark.parametrize("blind", ["ps", "lsof", "denied"])
def test_activity_probe_refuses_blind_or_denied_inventory(tmp_path, monkeypatch, blind):
    storage.write_json(tmp_path / "owner.json", {"token": "owner"})
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[0] == "ps":
            return subprocess.CompletedProcess(
                command, 0, "" if blind == "ps" else f"{os.getpid()} supervisor\n", ""
            )
        return subprocess.CompletedProcess(
            command, 2 if blind == "denied" else 0, "", "denied" if blind == "denied" else ""
        )

    monkeypatch.setattr(storage.subprocess, "run", run)
    with pytest.raises(storage.StorageBlocked):
        storage.idle(tmp_path)
    assert len(calls) == (1 if blind == "ps" else 2)


def test_strict_floor_and_measured_bootstrap_checked_before_any_staging(job):
    job.local_strict = True
    stage = storage.staging_bytes(job.source, job.runtime, job.state_source, 1024**2)
    with pytest.raises(storage.StorageBlocked, match='"floor_bytes": 28991029248'):
        storage.run_job(
            job,
            free=lambda _: max(
                storage.STRICT_RELEASE_FLOOR,
                storage.INITIAL_VERIFY_PEAK + storage.OBSERVED_OPERATING_MARGIN,
            )
            + stage
            - 1,
            probe=lambda _: None,
        )
    assert not list(job.temp_parent.glob("storage-verify-*"))


def test_nonlocal_ci_does_not_change_strict_local_floor(job):
    assert storage.STRICT_RELEASE_FLOOR == 27 * 1024**3
    assert storage.run_job(job, free=lambda _: storage.GIB, probe=lambda _: None) == 0
    assert receipt(job)["peak"] == 32


def test_malformed_or_symlinked_ledger_never_overwrites_unknown_data(tmp_path):
    manager = ledger(tmp_path)
    target = manager.directory / "reservations.json"
    target.write_text('{"jobs":null,"peaks":{}}')
    with pytest.raises(storage.StorageBlocked):
        manager.admit("one", "verify", 0, 1, owner_token="owner")
    assert target.read_text() == '{"jobs":null,"peaks":{}}'
    target.unlink()
    outside = tmp_path / "outside"
    outside.write_text("preserve")
    target.symlink_to(outside)
    with pytest.raises(storage.StorageBlocked, match="symlink"):
        manager.admit("one", "verify", 0, 1, owner_token="owner")
    assert outside.read_text() == "preserve"


def test_peak_budget_is_not_added_twice_to_launch_floor(tmp_path):
    manager = storage.Reservations(
        tmp_path / "control", tmp_path, floor=27, margin=5, free=lambda _: 31
    )
    manager.admit("first", "verify", 3, 22, owner_token="owner")
    with pytest.raises(storage.StorageBlocked, match='"required_bytes": 55'):
        manager.admit("second", "verify", 3, 22, owner_token="other")


def test_larger_producer_forecast_raises_early_reserve(tmp_path):
    manager = storage.Reservations(
        tmp_path / "control", tmp_path, floor=27, margin=5, free=lambda _: 40
    )
    with pytest.raises(storage.StorageBlocked, match='"required_bytes": 43'):
        manager.admit("large", "verify", 3, 35, owner_token="owner")


def test_known_other_device_does_not_create_a_global_storage_hold(tmp_path):
    manager = ledger(tmp_path, free=100)
    manager.admit("one", "verify", 10, 25, owner_token="owner")
    other = SimpleNamespace(stat=lambda: SimpleNamespace(st_dev=tmp_path.stat().st_dev + 1))
    separate = storage.Reservations(manager.directory, other, floor=40, free=lambda _: 60)
    separate.admit("two", "verify", 10, 25, owner_token="owner")


def test_source_or_release_claim_blocks_duplicates_even_with_plenty_of_space(tmp_path):
    manager = ledger(tmp_path, free=10**9)
    manager.admit("one", "verify", 1, 2, owner_token="owner", scope="release-target")
    with pytest.raises(storage.StorageBlocked, match="already claimed"):
        manager.admit("two", "verify", 1, 2, owner_token="other", scope="release-target")
    manager.finish("one", {"error": "uncertain"}, terminal=False)
    with pytest.raises(storage.StorageBlocked, match="already claimed"):
        manager.admit("three", "verify", 1, 2, owner_token="other", scope="release-target")
    manager.admit("independent", "verify", 1, 2, owner_token="other", scope="other-target")
    manager.finish("one", {"status": "terminal"}, terminal=True)
    manager.admit("four", "verify", 1, 2, owner_token="other", scope="release-target")


def test_release_key_blocks_changed_candidate_but_not_independent_owner(job, monkeypatch):
    monkeypatch.setenv("VERIFY_BEFORE_SYNC_OWNER_KEY", "publication-A")
    with pytest.raises(PermissionError):
        storage.run_job(
            job,
            free=lambda _: storage.GIB,
            probe=lambda _: (_ for _ in ()).throw(PermissionError()),
        )
    (job.source / "identity").write_text("successor")
    with pytest.raises(storage.StorageBlocked, match="already claimed"):
        storage.run_job(job, free=lambda _: storage.GIB, probe=lambda _: None)
    monkeypatch.setenv("VERIFY_BEFORE_SYNC_OWNER_KEY", "publication-B")
    assert storage.run_job(job, free=lambda _: storage.GIB, probe=lambda _: None) == 0


def test_git_object_staging_counts_docs_absent_from_working_tree(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.name", "test"),
        ("config", "user.email", "test@example.invalid"),
    ):
        subprocess.run(["git", "-C", str(source), *args], check=True)
    (source / "docs").mkdir()
    document = source / "docs/retained.bin"
    document.write_bytes(b"retained" * 8192)
    subprocess.run(["git", "-C", str(source), "add", "docs"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "fixture"], check=True)
    document.unlink()
    assert storage.staging_bytes(source, tmp_path / "absent", tmp_path / "absent", 100) >= 2 * 65536


def test_feedback_and_external_db_are_counted_even_over_directory_cap(tmp_path, monkeypatch):
    source, runtime = tmp_path / "source", tmp_path / "runtime"
    source.mkdir()
    (runtime / "feedback").mkdir(parents=True)
    (runtime / "feedback/brain.db").write_bytes(b"x" * 8192)
    external = tmp_path / "external.db"
    external.write_bytes(b"y" * 16384)
    monkeypatch.setenv("ORCH_FEEDBACK_DB", str(external))
    assert storage.staging_bytes(source, runtime, runtime, 100) >= 8192 + 16384


def test_sparse_or_hardlinked_state_under_allocated_copy_cap_reserves_materialization(tmp_path):
    source, runtime = tmp_path / "source", tmp_path / "runtime"
    source.mkdir()
    directory = runtime / "copied"
    directory.mkdir(parents=True)
    payload = directory / "a"
    payload.write_bytes(b"x" * 8192)
    (directory / "b").hardlink_to(payload)
    assert storage.footprint(directory)["logical"] == 8192
    assert storage.copy_bytes(directory) >= 16384
    assert storage.staging_bytes(source, runtime, runtime, 2**20) >= 16384


def test_replacement_during_activity_probe_is_never_renamed(tmp_path):
    root = tmp_path / "owned"
    root.mkdir()
    storage.write_json(root / "owner.json", {"token": "owner"})
    original = root.stat()

    def replace(path):
        path.rename(tmp_path / "original")
        path.mkdir()
        storage.write_json(path / "owner.json", {"token": "owner"})

    with pytest.raises(storage.StorageBlocked, match="ownership changed"):
        storage.retire(root, (original.st_dev, original.st_ino), "owner", replace)
    assert root.is_dir() and not root.with_name(root.name + ".retiring").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses the readonly filesystem fixture")
def test_readonly_fixture_is_preserved_while_writable_siblings_are_retired(tmp_path):
    root = tmp_path / "owned"
    root.mkdir()
    blocked = root / "readonly"
    blocked.mkdir()
    (blocked / "tool.sh").write_bytes(b"preserve")
    (root / "bulk").mkdir()
    (root / "bulk/payload").write_bytes(b"disposable")
    storage.write_json(root / "owner.json", {"token": "owner"})
    original = root.stat()
    blocked.chmod(0o555)  # Only this newly-created synthetic fixture.
    residual = root.with_name(root.name + ".retiring")
    try:
        paths = storage.retire(root, (original.st_dev, original.st_ino), "owner", lambda _: None)
        assert paths and str(residual / "readonly/tool.sh") in paths
        assert not (residual / "bulk").exists()
        assert (residual / "readonly/tool.sh").read_bytes() == b"preserve"
        assert (residual / "readonly").stat().st_mode & 0o777 == 0o555
        assert json.loads((residual / "owner.json").read_text())["token"] == "owner"
    finally:
        # Test teardown only; production retirement never changes permissions.
        if (residual / "readonly").exists():
            (residual / "readonly").chmod(0o755)


def test_unexpected_removal_failure_is_not_silenced(tmp_path, monkeypatch):
    root = tmp_path / "owned"
    root.mkdir()
    storage.write_json(root / "owner.json", {"token": "owner"})
    original = root.stat()

    def fail(path, *, onexc):
        onexc(os.unlink, str(path / "unknown"), OSError(5, "unexpected IO failure"))

    fail.avoids_symlink_attacks = True
    monkeypatch.setattr(storage.shutil, "rmtree", fail)
    with pytest.raises(OSError, match="unexpected IO"):
        storage.retire(root, (original.st_dev, original.st_ino), "owner", lambda _: None)
