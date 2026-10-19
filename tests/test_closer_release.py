from __future__ import annotations

import json

import pytest

from src.closer_release import ReleaseConflict, ReleaseError, ReleaseRecord

CANDIDATE = "a" * 40
INCUMBENT = "generation-v1:" + "b" * 64
DIGEST = "c" * 64
GENERATION = "generation-v1:" + "d" * 64


def start_verified(record: ReleaseRecord, tmp_path):
    state = record.begin(CANDIDATE, INCUMBENT, "closer", run_id="run-1")
    verifying = record.transition("verifying", lane_owner="closer", expected_revision=state["revision"])
    return record.record_verification(
        lane_owner="closer", expected_revision=verifying["revision"], verified=True,
        source_identity="e" * 64, snapshot_path=tmp_path / "snapshot",
        snapshot_digest=DIGEST, log_path=tmp_path / "verify.log", exit_code=0,
    )


def test_record_survives_restart_and_preserves_original_incumbent_token(tmp_path):
    path = tmp_path / "release.json"
    first = ReleaseRecord(path)
    initial = first.begin(CANDIDATE, INCUMBENT, "closer", run_id="run-1")
    verifying = first.transition("verifying", lane_owner="closer", expected_revision=initial["revision"])
    verified = first.record_verification(
        lane_owner="closer", expected_revision=verifying["revision"], verified=True,
        source_identity="e" * 64, snapshot_path=tmp_path / "verified", snapshot_digest=DIGEST,
        log_path=tmp_path / "verify.log", exit_code=0,
    )

    restarted = ReleaseRecord(path)
    resumed = restarted.begin(CANDIDATE, "generation-v1:" + "f" * 64, "closer")
    assert resumed["revision"] == verified["revision"]
    assert resumed["incumbent_token"] == INCUMBENT
    assert restarted.read()["verification"]["snapshot_digest"] == DIGEST


def test_competing_publisher_cannot_take_active_record(tmp_path):
    record = ReleaseRecord(tmp_path / "release.json")
    record.begin(CANDIDATE, INCUMBENT, "closer")
    with pytest.raises(ReleaseConflict, match="owned by another"):
        record.begin("f" * 40, INCUMBENT, "different-lane")
    with pytest.raises(ReleaseConflict, match="ownership"):
        record.transition("verifying", lane_owner="different-lane", expected_revision=1)


def test_failed_verification_is_durable_and_cannot_install(tmp_path):
    record = ReleaseRecord(tmp_path / "release.json")
    initial = record.begin(CANDIDATE, INCUMBENT, "closer")
    verifying = record.transition("verifying", lane_owner="closer", expected_revision=initial["revision"])
    failed = record.record_verification(
        lane_owner="closer", expected_revision=verifying["revision"], verified=False,
        source_identity=None, snapshot_path=None, snapshot_digest=None,
        log_path=tmp_path / "verify.log", exit_code=1,
    )
    assert failed["stage"] == "failed"
    assert ReleaseRecord(record.path).read()["failure"] == {"stage": "verification", "exit_code": 1}
    with pytest.raises(ReleaseError, match="illegal release transition"):
        record.transition("installing", lane_owner="closer", expected_revision=failed["revision"])


def test_install_acceptance_rollback_stages_and_revision_cas(tmp_path):
    record = ReleaseRecord(tmp_path / "release.json")
    verified = start_verified(record, tmp_path)
    installing = record.transition("installing", lane_owner="closer", expected_revision=verified["revision"])
    installed = record.transition("installed", lane_owner="closer", expected_revision=installing["revision"],
                                  installed_generation=GENERATION)
    pending = record.transition("acceptance_pending", lane_owner="closer", expected_revision=installed["revision"])
    accepted = record.transition("accepted", lane_owner="closer", expected_revision=pending["revision"],
                                 live_acceptance={"accepted": True, "evidence": "health check passed"})
    assert accepted["stage"] == "accepted"
    with pytest.raises(ReleaseConflict, match="revision changed"):
        record.transition("rollback_pending", lane_owner="closer", expected_revision=installed["revision"])

    rollback_record = ReleaseRecord(tmp_path / "rollback.json")
    verified2 = start_verified(rollback_record, tmp_path)
    installing2 = rollback_record.transition("installing", lane_owner="closer", expected_revision=verified2["revision"])
    installed2 = rollback_record.transition("installed", lane_owner="closer", expected_revision=installing2["revision"],
                                             installed_generation=GENERATION)
    rollback = rollback_record.transition("rollback_pending", lane_owner="closer", expected_revision=installed2["revision"],
                                          rollback={"reason": "acceptance failed", "restore_token": INCUMBENT})
    done = rollback_record.transition("rolled_back", lane_owner="closer", expected_revision=rollback["revision"])
    assert done["rollback"]["restore_token"] == INCUMBENT


def test_record_is_bounded_and_atomic_json(tmp_path):
    record = ReleaseRecord(tmp_path / "release.json")
    record.begin(CANDIDATE, INCUMBENT, "closer")
    assert json.loads(record.path.read_text())["schema_version"] == 1
    assert not list(tmp_path.glob(".release.json.*.tmp"))
    record.path.write_text("x" * (64 * 1024 + 1))
    with pytest.raises(ReleaseError, match="size bound"):
        record.read()
