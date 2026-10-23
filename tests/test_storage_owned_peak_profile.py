"""Owned calibration cannot relabel plans, shared pressure, or terminal size as peak."""

import hashlib
import json
import subprocess
from types import SimpleNamespace

import pytest
from test_storage_lifecycle import job as job

import storage_lifecycle as storage


def manager(tmp_path, free=10000):
    return storage.Reservations(
        tmp_path / "ledger", tmp_path, floor=40, margin=5, free=lambda _: free
    )


def contract(tmp_path, revision="fixture-copy-1"):
    return storage.measurement_profile(tmp_path, "verify", revision, tmp_path)


def owned(tmp_path):
    root = tmp_path / "owned"
    root.mkdir(mode=0o700)
    storage.write_json(root / "owner.json", {"token": "owner"})
    st = root.stat()
    return root, storage.OwnedHighwater(root, (st.st_dev, st.st_ino), "owner")


def finish(m, job, sample, **kwargs):
    m.finish(
        job,
        {"owned_highwater": sample, "source_unchanged": True, "exit_code": 0, **kwargs},
        terminal=True,
    )


def test_shared_volume_decline_and_plan_never_become_measured_peak(tmp_path):
    m = manager(tmp_path)
    m.admit("one", "verify", 10, 25, owner_token="owner", measurement_contract=contract(tmp_path))
    m.finish(
        "one", {"observed_growth_bytes": 9000, "shared_volume_decline_bytes": 9000}, terminal=True
    )
    data = json.loads((m.directory / "reservations.json").read_text())
    assert data["peaks"] == {}
    assert not data.get("owned_measurements")
    assert m.admit("two", "verify", 10, 25, owner_token="owner")["peak"] == 25


def test_sample_before_early_cleanup_survives_smaller_terminal_sample(tmp_path):
    root, sampler = owned(tmp_path)
    (root / "fixture").write_bytes(b"x" * 50000)
    sampler.sample(force=True)
    high = sampler.result["allocated_highwater_bytes"]
    (root / "fixture").unlink()
    sampler.sample(force=True)
    assert sampler.result["allocated_highwater_bytes"] == high
    assert sampler.result["logical_highwater_bytes"] >= 50000
    assert sampler.result["peak_bound_proven"] is False
    assert sampler.result["scope"] == "total-owned-including-staging"


def test_sampling_is_rate_limited_not_a_recursive_busy_loop(tmp_path, monkeypatch):
    root, sampler = owned(tmp_path)
    sampler.sample()
    monkeypatch.setattr(storage.os, "walk", lambda *a, **k: pytest.fail("unexpected rescan"))
    sampler.sample()
    assert sampler.result["complete_samples"] == 1
    assert root.exists()


def test_transient_between_samples_is_not_claimed_as_calibrated_peak(tmp_path):
    root, sampler = owned(tmp_path)
    sampler.sample(force=True)
    (root / "transient").write_bytes(b"x" * 50000)
    (root / "transient").unlink()
    sampler.sample(force=True)
    m = manager(tmp_path, free=100000)
    row = m.admit(
        "one", "verify", 0, 25000, owner_token="owner", measurement_contract=contract(tmp_path)
    )
    finish(m, "one", sampler.result)
    assert row["calibration_status"] == "insufficient_calibration"
    row = m.admit(
        "two", "verify", 0, 25000, owner_token="owner", measurement_contract=contract(tmp_path)
    )
    assert row["peak"] >= 25000
    assert row["calibration_status"] == "insufficient_calibration"


def test_profile_rollover_preserves_history_but_never_reuses_stale_sample(tmp_path):
    m = manager(tmp_path)
    first, second = contract(tmp_path), contract(tmp_path, "fixture-copy-2")
    assert first["sha256"] != second["sha256"]
    m.admit("one", "verify", 0, 25, owner_token="owner", measurement_contract=first)
    sample = {
        "schema": storage.OWNED_SAMPLE_CONTRACT,
        "complete_samples": 2,
        "failed_samples": 0,
        "allocated_highwater_bytes": 90,
    }
    finish(m, "one", sample)
    assert (
        m.admit("two", "verify", 0, 25, owner_token="owner", measurement_contract=first)["peak"]
        == 90
    )
    assert (
        m.admit("three", "verify", 0, 25, owner_token="owner", measurement_contract=second)["peak"]
        == 25
    )
    data = json.loads((m.directory / "reservations.json").read_text())
    assert data["owned_measurements"][first["sha256"]]["allocated_highwater_bytes"] == 90


def test_old_legacy_estimates_and_unknown_concurrent_reservations_stay_charged(tmp_path):
    m = manager(tmp_path, free=105)
    storage.write_json(
        m.directory / "reservations.json",
        {"jobs": {"old": {"remaining": 30, "status": "uncertain"}}, "peaks": {"verify": 50}},
    )
    row = m.admit(
        "one", "verify", 20, 25, owner_token="owner", measurement_contract=contract(tmp_path)
    )
    assert row["peak"] == 50
    assert row["remaining"] == 70
    with pytest.raises(storage.StorageBlocked, match="other_reserved_bytes"):
        m.admit("two", "verify", 0, 25, owner_token="other")


@pytest.mark.parametrize("failure", ["permission", "raced", "bound", "identity"])
def test_failed_or_incomplete_owned_scan_cannot_learn(tmp_path, monkeypatch, failure):
    root, sampler = owned(tmp_path)
    sampler.sample(force=True)
    if failure in ("permission", "raced"):

        def fail(*args, **kwargs):
            raise (
                PermissionError("denied") if failure == "permission" else FileNotFoundError("raced")
            )

        monkeypatch.setattr(storage.os, "walk", fail)
    elif failure == "bound":
        (root / "fixture").write_text("data")
        monkeypatch.setattr(storage, "SAMPLE_MAX_ENTRIES", 1)
    else:
        sampler.identity = (0, 0)
    sampler.sample(force=True)
    assert sampler.result["failed_samples"] == 1
    m = manager(tmp_path, free=100000)
    m.admit("one", "verify", 0, 25, owner_token="owner", measurement_contract=contract(tmp_path))
    finish(m, "one", sampler.result)
    assert not json.loads((m.directory / "reservations.json").read_text()).get("owned_measurements")


def test_interrupted_reservation_retains_charge_without_learning(tmp_path):
    m = manager(tmp_path)
    m.admit("one", "verify", 10, 25, owner_token="owner", measurement_contract=contract(tmp_path))
    m.finish("one", {"owned_highwater": {"allocated_highwater_bytes": 2}}, terminal=False)
    data = json.loads((m.directory / "reservations.json").read_text())
    assert data["jobs"]["one"]["remaining"] == 35
    assert data["jobs"]["one"]["status"] == "uncertain"
    assert not data.get("owned_measurements")


def test_owned_sampler_does_not_follow_external_symlink_or_double_count_hardlinks(tmp_path):
    root, sampler = owned(tmp_path)
    outside = tmp_path / "shared-cache"
    outside.write_bytes(b"x" * 50000)
    (root / "link").symlink_to(outside)
    (root / "small").write_bytes(b"x" * 100)
    (root / "hardlink").hardlink_to(root / "small")
    sampler.sample(force=True)
    assert sampler.result["logical_highwater_bytes"] < 1000
    assert outside.read_bytes() == b"x" * 50000


def test_copy_producer_or_platform_change_versions_profile(tmp_path, monkeypatch):
    script = tmp_path / "scripts"
    script.mkdir()
    (script / "build_exec_mirror.sh").write_text("copy one")
    first = contract(tmp_path)
    (script / "build_exec_mirror.sh").write_text("copy two")
    second = contract(tmp_path)
    assert first["sha256"] != second["sha256"]
    monkeypatch.setattr(storage.platform, "release", lambda: "different-os")
    assert contract(tmp_path)["sha256"] != second["sha256"]


def test_existing_supervisor_durably_records_strict_owned_scope_without_changing_floor(job):
    job.local_strict = True
    assert storage.run_job(job, free=lambda _: 100 * storage.GIB, probe=lambda _: None) == 0
    records = list((job.state_dir / "receipts").glob("*.json"))
    assert len(records) == 1
    receipt = json.loads(records[0].read_text())
    assert receipt["owned_highwater"]["complete_samples"] >= 2
    assert receipt["owned_highwater"]["peak_bound_proven"] is False
    assert receipt["measurement_contract"]["fixture_copy_identity"] == receipt["source_identity"]
    assert receipt["calibration_status"] == "insufficient_calibration"
    assert receipt["peak"] >= storage.INITIAL_VERIFY_PEAK
    assert storage.STRICT_RELEASE_FLOOR == 27 * storage.GIB
    assert not list(job.temp_parent.glob("storage-verify-*"))
    ledger = json.loads((job.state_dir / "reservations.json").read_text())
    assert ledger["peaks"] == {}
    assert next(iter(ledger["owned_measurements"].values()))["peak_bound_proven"] is False


def test_non_strict_job_never_calibrates_from_small_scratch(job):
    assert storage.run_job(job, free=lambda _: storage.GIB, probe=lambda _: None) == 0
    ledger = json.loads((job.state_dir / "reservations.json").read_text())
    assert not ledger.get("owned_measurements")


def test_staged_checkpoint_retains_copy_footprint_before_early_cleanup(tmp_path):
    root, sampler = owned(tmp_path)
    m = manager(tmp_path, free=1000000)
    m.admit("one", "verify", 100, 25, owner_token="owner", measurement_contract=contract(tmp_path))
    m.bind("one", root=str(root), root_identity=list(sampler.identity))
    (root / "staging-copy").write_bytes(b"x" * 50000)
    m.staged("one", "owner")
    (root / "staging-copy").unlink()
    sampler.sample(force=True)
    finish(m, "one", sampler.result)
    data = json.loads((m.directory / "reservations.json").read_text())
    measurement = next(iter(data["owned_measurements"].values()))
    assert measurement["allocated_highwater_bytes"] >= 50000
    assert measurement["peak_bound_proven"] is False
    receipt = json.loads((m.directory / "receipts/one.json").read_text())
    assert receipt["staged_owned_checkpoint"]["logical_highwater_bytes"] >= 50000
    assert receipt["owned_highwater"]["logical_highwater_bytes"] < 50000


def test_bad_owned_calibration_cannot_optimistically_admit(tmp_path):
    m = manager(tmp_path)
    key = contract(tmp_path)["sha256"]
    storage.write_json(
        m.directory / "reservations.json",
        {
            "jobs": {},
            "peaks": {},
            "owned_measurements": {
                key: {"contract": {"sha256": key}, "allocated_highwater_bytes": 0}
            },
        },
    )
    with pytest.raises(storage.StorageBlocked, match="contract mismatch"):
        m.admit(
            "one", "verify", 0, 25, owner_token="owner", measurement_contract=contract(tmp_path)
        )


@pytest.mark.parametrize("value", [-1, None, True, "small"])
def test_malformed_owned_measurement_stays_unchanged_and_blocks_admission(tmp_path, value):
    m = manager(tmp_path)
    profile = contract(tmp_path)
    path = m.directory / "reservations.json"
    storage.write_json(
        path,
        {
            "jobs": {},
            "peaks": {},
            "owned_measurements": {
                profile["sha256"]: {"contract": profile, "allocated_highwater_bytes": value}
            },
        },
    )
    before = path.read_bytes()
    with pytest.raises(storage.StorageBlocked, match="invalid owned measurement record"):
        m.admit("one", "verify", 0, 25, owner_token="owner", measurement_contract=profile)
    assert path.read_bytes() == before


@pytest.fixture
def engineering_contract(tmp_path, monkeypatch):
    source = tmp_path / "cold-source"
    source.mkdir()
    producer = source / "copy-producer.py"
    producer.write_text("copy only bounded fixture modules")
    copier = tmp_path / "copy.sh"
    copier.write_text("reviewed copier")
    monkeypatch.setattr(
        storage,
        "REVIEWED_PRODUCERS",
        {producer.name: hashlib.sha256(producer.read_bytes()).hexdigest()},
    )
    monkeypatch.setattr(
        storage, "REVIEWED_EXTERNAL_COPY_SHA256", hashlib.sha256(copier.read_bytes()).hexdigest()
    )
    monkeypatch.setattr(storage.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(storage.platform, "release", lambda: "25.6.0")
    monkeypatch.setattr(storage.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(storage.sys, "version_info", (3, 12, 2))
    monkeypatch.setenv("ORCH_SYNC_SCRIPT", str(copier))
    monkeypatch.setattr(storage, "_archived_producers_match", lambda source: True)
    return source, producer, copier


def test_reviewed_engineering_plan_is_not_a_measured_peak(engineering_contract):
    source, _producer, _copier = engineering_contract
    plan = storage.reviewed_cold_plan(source, "EXEC MIRROR", 50)
    assert plan["kind"] == "reviewed_engineering_estimate"
    assert plan["measured_peak"] is False
    assert plan["baseline_total_bytes"] == 12_442_103_808
    assert (
        plan["runtime_component_bytes"] + plan["baseline_staging_bytes"]
        == plan["shared_window_bytes"]
    )
    assert len(plan["receipt_sha256"]) == 4


@pytest.mark.parametrize(
    "change", ["producer", "missing", "symlink", "copier", "mode", "maximum", "os", "python"]
)
def test_declared_contract_changes_fall_back_conservatively(
    engineering_contract, monkeypatch, change
):
    source, producer, copier = engineering_contract
    profile, maximum = "EXEC MIRROR", 50
    if change == "producer":
        producer.write_text("unreviewed full directory copy")
    elif change == "missing":
        producer.unlink()
    elif change == "symlink":
        raw = producer.read_text()
        producer.unlink()
        outside = source.parent / "outside-producer"
        outside.write_text(raw)
        producer.symlink_to(outside)
    elif change == "copier":
        copier.write_text("changed copier")
    elif change == "mode":
        profile = "BARE EXEC MIRROR"
    elif change == "maximum":
        maximum = 51
    elif change == "os":
        monkeypatch.setattr(storage.platform, "release", lambda: "changed")
    else:
        monkeypatch.setattr(storage.sys, "version_info", (3, 13, 0))
    assert storage.reviewed_cold_plan(source, profile, maximum) is None


@pytest.mark.parametrize("growth", [0, 123456])
def test_engineering_plan_counts_baseline_staging_once_and_growth_fully(
    engineering_contract, job, monkeypatch, growth
):
    source, _producer, _copier = engineering_contract
    plan = storage.reviewed_cold_plan(source, "EXEC MIRROR", 50)
    monkeypatch.setattr(storage, "_reviewed_command_matches", lambda args: True)
    monkeypatch.setattr(storage, "reviewed_cold_plan", lambda *args: plan)
    monkeypatch.setattr(
        storage, "staging_bytes", lambda *args, **kwargs: storage.REVIEWED_STAGING_BYTES + growth
    )
    job.local_strict = True
    required = plan["baseline_total_bytes"] + growth
    with pytest.raises(storage.StorageBlocked, match=f'"required_bytes": {required}'):
        storage.run_job(job, free=lambda _: required - 1, probe=lambda _: None)
    assert not list(job.temp_parent.glob("storage-verify-*"))
    assert storage.run_job(job, free=lambda _: required, probe=lambda _: None) == 0
    receipt = json.loads(next((job.state_dir / "receipts").glob("*.json")).read_text())
    assert receipt["profile"].endswith(plan["profile_key"])
    assert receipt["peak"] == 6_200_139_776
    assert receipt["staging"] == 611_594_240 + growth
    assert receipt["resource_plan"]["measured_peak"] is False
    assert receipt["calibration_status"] == "insufficient_calibration"


def test_engineering_plan_preserves_legacy_forecasts_and_active_unknown_charges(
    engineering_contract, job, monkeypatch
):
    source, _producer, _copier = engineering_contract
    plan = storage.reviewed_cold_plan(source, "EXEC MIRROR", 50)
    monkeypatch.setattr(storage, "_reviewed_command_matches", lambda args: True)
    monkeypatch.setattr(storage, "reviewed_cold_plan", lambda *args: plan)
    monkeypatch.setattr(
        storage, "staging_bytes", lambda *args, **kwargs: storage.REVIEWED_STAGING_BYTES
    )
    job.local_strict = True
    m = storage.Reservations(job.state_dir, job.temp_parent, floor=0)
    storage.write_json(
        m.directory / "reservations.json",
        {
            "jobs": {"legacy": {"remaining": 100, "status": "uncertain"}},
            "peaks": {job.profile: storage.INITIAL_VERIFY_PEAK},
        },
    )
    required = plan["baseline_total_bytes"] + 100
    with pytest.raises(storage.StorageBlocked, match='"other_reserved_bytes": 100'):
        storage.run_job(job, free=lambda _: required - 1, probe=lambda _: None)
    assert storage.run_job(job, free=lambda _: required, probe=lambda _: None) == 0
    data = json.loads((m.directory / "reservations.json").read_text())
    assert data["jobs"]["legacy"] == {"remaining": 100, "status": "uncertain"}
    assert data["peaks"][job.profile] == storage.INITIAL_VERIFY_PEAK


def test_engineering_producer_drift_before_admission_has_no_child(
    engineering_contract, job, monkeypatch
):
    source, _producer, _copier = engineering_contract
    plan = storage.reviewed_cold_plan(source, "EXEC MIRROR", 50)
    calls = iter([plan, None])
    monkeypatch.setattr(storage, "_reviewed_command_matches", lambda args: True)
    monkeypatch.setattr(storage, "reviewed_cold_plan", lambda *args: next(calls))
    job.local_strict = True
    with pytest.raises(storage.StorageBlocked, match="changed before admission"):
        storage.run_job(job, free=lambda _: 100 * storage.GIB, probe=lambda _: None)
    assert not list(job.temp_parent.glob("storage-verify-*"))
    assert not (job.state_dir / "reservations.json").exists()


def test_engineering_runtime_estimate_not_lowered_by_failed_samples(engineering_contract, tmp_path):
    source, _producer, _copier = engineering_contract
    plan = storage.reviewed_cold_plan(source, "EXEC MIRROR", 50)
    m = manager(tmp_path, free=100 * storage.GIB)
    key = "EXEC MIRROR:" + plan["profile_key"]
    profile = contract(tmp_path)
    m.admit(
        "one",
        key,
        storage.REVIEWED_STAGING_BYTES,
        plan["runtime_component_bytes"],
        owner_token="owner",
        measurement_contract=profile,
    )
    finish(
        m,
        "one",
        {
            "schema": storage.OWNED_SAMPLE_CONTRACT,
            "complete_samples": 1,
            "failed_samples": 1,
            "allocated_highwater_bytes": 1,
        },
    )
    row = m.admit(
        "two",
        key,
        storage.REVIEWED_STAGING_BYTES,
        plan["runtime_component_bytes"],
        owner_token="owner",
        measurement_contract=profile,
    )
    assert row["peak"] == plan["runtime_component_bytes"]
    assert row["calibration_status"] == "insufficient_calibration"


def test_archived_producer_uses_head_not_uncommitted_known_bytes(tmp_path, monkeypatch):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    producer = scripts / "copy.sh"
    producer.write_text("old committed producer")

    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q")
    git("add", "scripts/copy.sh")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "fixture producer",
    )
    monkeypatch.setattr(
        storage,
        "REVIEWED_PRODUCERS",
        {"scripts/copy.sh": hashlib.sha256(producer.read_bytes()).hexdigest()},
    )
    assert storage._archived_producers_match(tmp_path) is True
    producer.write_text("new known working bytes")
    monkeypatch.setattr(
        storage,
        "REVIEWED_PRODUCERS",
        {"scripts/copy.sh": hashlib.sha256(producer.read_bytes()).hexdigest()},
    )
    assert storage._archived_producers_match(tmp_path) is False
    git("add", "scripts/copy.sh")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "reviewed fixture",
    )
    assert storage._archived_producers_match(tmp_path) is True
    monkeypatch.setattr(storage, "REVIEWED_PRODUCERS", {"scripts/missing.sh": "0" * 64})
    assert storage._archived_producers_match(tmp_path) is False


def test_archive_inventory_failure_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "REVIEWED_PRODUCERS", {"scripts/copy.sh": "0" * 64})

    def failed(*args, **kwargs):
        raise subprocess.TimeoutExpired("git", 10)

    monkeypatch.setattr(storage.subprocess, "run", failed)
    assert storage._archived_producers_match(tmp_path) is False


def test_larger_explicit_plan_wins_over_engineering_estimate(
    engineering_contract, job, monkeypatch
):
    source, _producer, _copier = engineering_contract
    plan = storage.reviewed_cold_plan(source, "EXEC MIRROR", 50)
    monkeypatch.setattr(storage, "_reviewed_command_matches", lambda args: True)
    monkeypatch.setattr(storage, "reviewed_cold_plan", lambda *args: plan)
    job.local_strict = True
    job.peak_bytes = plan["runtime_component_bytes"] + storage.GIB
    assert storage.run_job(job, free=lambda _: 100 * storage.GIB, probe=lambda _: None) == 0
    receipt = json.loads(next((job.state_dir / "receipts").glob("*.json")).read_text())
    assert receipt["peak"] == job.peak_bytes


@pytest.fixture
def command_contract(tmp_path, monkeypatch):
    source = tmp_path / "command-source"
    source.mkdir()
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    args = SimpleNamespace(
        source=source,
        runtime=runtime,
        state_source=state,
        local_strict=True,
        profile="EXEC MIRROR",
        max_dir_mb=50,
        command=["bash", str(source / "scripts/verify_before_sync.sh"), str(source)],
    )
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(runtime))
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setenv("PYTHON", storage.sys.executable)
    registry = tmp_path / "registry.json"
    registry.write_text("{}")
    monkeypatch.setenv("ORCH_REVIEW_REGISTRY_SNAPSHOT", str(registry))
    for name in (
        "VERIFY_BEFORE_SYNC_TREE",
        "VERIFY_BEFORE_SYNC_MAX_DIR_MB",
        "PYTEST_ADDOPTS",
        "PYTEST_DEBUG_TEMPROOT",
    ):
        monkeypatch.delenv(name, raising=False)
    return args


def test_reviewed_canonical_command_accepts_only_supported_copy_options(command_contract):
    args = command_contract
    assert storage._reviewed_command_matches(args)
    args.command[2:2] = [
        "--snapshot-out",
        "/tmp/example-snapshot",
        "--digest-out",
        "/tmp/example-digest",
    ]
    assert storage._reviewed_command_matches(args)


@pytest.mark.parametrize(
    "change",
    [
        "other_script",
        "other_source",
        "identity",
        "unsupported_args",
        "digest_without_snapshot",
        "mode_env",
        "max_env",
        "runtime_env",
        "python_env",
        "pytest_env",
        "temp_env",
        "registry_missing",
    ],
)
def test_workload_or_interpreter_mismatch_never_gets_engineering_forecast(
    command_contract, monkeypatch, change
):
    args = command_contract
    if change == "other_script":
        args.command[1] = str(args.source / "other-large-workload.sh")
    elif change == "other_source":
        args.command[-1] = str(args.source / "other-source")
    elif change == "identity":
        args.command.insert(2, "--identity")
    elif change == "unsupported_args":
        args.command.insert(2, "--arbitrary-workload")
    elif change == "digest_without_snapshot":
        args.command[2:2] = ["--digest-out", "/tmp/example-digest"]
    else:
        key, value = {
            "mode_env": ("VERIFY_BEFORE_SYNC_TREE", "BARE EXEC MIRROR"),
            "max_env": ("VERIFY_BEFORE_SYNC_MAX_DIR_MB", "500"),
            "runtime_env": ("ORCH_LOCAL_RUNTIME", str(args.runtime / "other")),
            "python_env": ("PYTHON", "/unknown/python"),
            "pytest_env": ("PYTEST_ADDOPTS", "-n32"),
            "temp_env": ("PYTEST_DEBUG_TEMPROOT", "/unknown/tmp"),
            "registry_missing": ("ORCH_REVIEW_REGISTRY_SNAPSHOT", "/missing/registry.json"),
        }[change]
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        storage,
        "reviewed_cold_plan",
        lambda *a: pytest.fail("unmatched workload must retain legacy plan"),
    )
    assert storage._qualified_cold_plan(args) is None


def test_registry_copy_growth_is_fully_forecasted_not_a_remote_placeholder(job, monkeypatch):
    monkeypatch.delenv("ORCH_REVIEW_REGISTRY_SNAPSHOT", raising=False)
    baseline = storage.staging_bytes(job.source, job.runtime, job.state_source, 50 * 1024**2)
    registry = job.source / "captured-registry.json"
    registry.write_bytes(b"x" * 9000)
    monkeypatch.setenv("ORCH_REVIEW_REGISTRY_SNAPSHOT", str(registry))
    observed = storage.staging_bytes(job.source, job.runtime, job.state_source, 50 * 1024**2)
    assert observed - baseline == 2 * storage.copy_bytes(registry)


def test_unknown_network_registry_cannot_qualify_engineering_plan(command_contract, monkeypatch):
    monkeypatch.delenv("ORCH_REVIEW_REGISTRY_SNAPSHOT")
    assert storage._qualified_cold_plan(command_contract) is None


def test_plan_metadata_io_failure_is_guarded_known_no_spawn(job, monkeypatch):
    bind = storage.Reservations.bind

    def fail_metadata(self, job_id, **metadata):
        if "resource_plan" in metadata:
            raise PermissionError("metadata write refused")
        return bind(self, job_id, **metadata)

    monkeypatch.setattr(storage.Reservations, "bind", fail_metadata)
    monkeypatch.setattr(storage, "identity", lambda _: "original")
    monkeypatch.setattr(storage.subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn"))
    with pytest.raises(PermissionError, match="metadata write refused"):
        storage.run_job(job, free=lambda _: storage.GIB, probe=lambda _: None)
    record = json.loads(next((job.state_dir / "receipts").glob("*.json")).read_text())
    assert record["spawn_attempted"] is False
    assert record["status"] == "not_started_retired"
    assert not list(job.temp_parent.glob("storage-verify-*"))
    assert json.loads((job.state_dir / "reservations.json").read_text())["jobs"] == {}


def test_pre_spawn_contract_refusal_retires_only_proven_owned_root(
    engineering_contract, job, monkeypatch
):
    source, _producer, _copier = engineering_contract
    plan = storage.reviewed_cold_plan(source, "EXEC MIRROR", 50)
    calls = iter([plan, plan, None])
    monkeypatch.setattr(storage, "_qualified_cold_plan", lambda args: next(calls))
    monkeypatch.setattr(storage, "identity", lambda _: "original")
    monkeypatch.setattr(storage.subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn"))
    job.local_strict = True
    with pytest.raises(storage.StorageBlocked, match="changed before child launch"):
        storage.run_job(job, free=lambda _: 100 * storage.GIB, probe=lambda _: None)
    record = json.loads(next((job.state_dir / "receipts").glob("*.json")).read_text())
    assert record["spawn_attempted"] is False
    assert record["status"] == "not_started_retired"
    assert json.loads((job.state_dir / "reservations.json").read_text())["jobs"] == {}


@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_popen_constructor_uncertainty_never_counts_as_no_spawn(job, monkeypatch, error):
    monkeypatch.setattr(storage, "identity", lambda _: "original")

    def uncertain_constructor(*args, **kwargs):
        raise error("constructor may have forked")

    monkeypatch.setattr(storage.subprocess, "Popen", uncertain_constructor)
    with pytest.raises(error):
        storage.run_job(
            job,
            free=lambda _: storage.GIB,
            probe=lambda _: pytest.fail("must retain uncertain child storage"),
        )
    record = json.loads(next((job.state_dir / "receipts").glob("*.json")).read_text())
    assert record["spawn_attempted"] is True
    assert record["status"] == "retained_uncertain"
    assert list(job.temp_parent.glob("storage-verify-*"))
    assert json.loads((job.state_dir / "reservations.json").read_text())["jobs"]


def test_no_spawn_probe_denial_retains_storage_without_force(job, monkeypatch):
    monkeypatch.setattr(storage, "identity", lambda _: "original")

    def fail_bind(*args, **kwargs):
        raise PermissionError("metadata refused")

    monkeypatch.setattr(storage.Reservations, "bind", fail_bind)

    def denied_probe(root):
        raise PermissionError("inspection refused")

    with pytest.raises(PermissionError, match="metadata refused"):
        storage.run_job(job, free=lambda _: storage.GIB, probe=denied_probe)
    record = json.loads(next((job.state_dir / "receipts").glob("*.json")).read_text())
    assert record["spawn_attempted"] is False
    assert record["status"] == "not_started_cleanup_pending"
    assert record["cleanup_error"] == "PermissionError"
    assert list(job.temp_parent.glob("storage-verify-*"))
    assert json.loads((job.state_dir / "reservations.json").read_text())["jobs"]


def test_allocation_failure_before_any_spawn_releases_future_charge(job, monkeypatch):
    monkeypatch.setattr(storage, "identity", lambda _: "original")

    def failed_allocation(*args, **kwargs):
        raise OSError("allocation refused")

    monkeypatch.setattr(storage.tempfile, "mkdtemp", failed_allocation)
    with pytest.raises(OSError, match="allocation refused"):
        storage.run_job(
            job, free=lambda _: storage.GIB, probe=lambda _: pytest.fail("no root to retire")
        )
    record = json.loads(next((job.state_dir / "receipts").glob("*.json")).read_text())
    assert record["status"] == "not_started" and record["spawn_attempted"] is False
    assert record["root"] is None
    assert json.loads((job.state_dir / "reservations.json").read_text())["jobs"] == {}
