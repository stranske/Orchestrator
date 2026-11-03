"""release-interrupted drains a STAGED reservation whose run was interrupted before its verify ended.

Proof (same standard as abort reconciliation): owner PID, child PGID and the sealed release parent
are dead, no receipt exists, the owner marker binds the row, and the ps/lsof probe sees nothing in
the root. Anything less holds the reservation, with a reason.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
from pathlib import Path

import pytest

import storage_lifecycle as storage

JOB = "d6f61c93b1664eaf944cc1dd549ef0ba"
TOKEN = "owner-token"


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, sort_keys=True))
    path.chmod(0o600)


@pytest.fixture
def interrupted(tmp_path, monkeypatch):
    state = tmp_path / "state"
    root = tmp_path / "storage-verify-root"
    root.mkdir(mode=0o700)
    (root / "staging").mkdir()
    (root / "staging" / "bulk.bin").write_bytes(b"x" * 4096)
    _write(root / "owner.json", {"job": JOB, "token": TOKEN})
    source = tmp_path / "source"
    source.mkdir()
    verifier = source / "verify_before_sync.sh"
    verifier.write_text("#!/bin/bash\n")
    parent_command = f"bash /home/u/.codex/bin/orch-mirror-sync.sh {source}"
    manager = storage.Reservations(state, tmp_path, floor=0, free=lambda _: 10**9)
    manager.admit(JOB, "verify", 1, 1, owner_token=TOKEN, scope="one")
    manager.bind(
        JOB,
        root=str(root),
        root_identity=list((root.stat().st_dev, root.stat().st_ino)),
        source_identity="41183a3daba92aec6dae4989b01100dc9d5ea5db",
        source_command=["bash", str(verifier), "--identity", str(source)],
        child_pgid=29298,
    )
    (state / f"{JOB}.owner").touch(mode=0o600)
    row = json.loads((state / "reservations.json").read_text())["jobs"][JOB]
    binding = storage._stage_source_binding(
        job=JOB,
        row=row,
        child_command=["bash", str(verifier), "--snapshot-out", str(tmp_path / "s"), str(source)],
        release_parent={
            "pid": 29243,
            "ppid": 1,
            "pgid": 29242,
            "command": parent_command,
            "argv": shlex.split(parent_command),
            "command_sha256": hashlib.sha256(parent_command.encode()).hexdigest(),
        },
    )
    storage._create_private_json_once(storage._stage_source_binding_path(state, JOB), binding)
    manager.bind(
        JOB,
        stage_source_binding_sha256=storage._sha256_bytes(
            json.dumps(binding, sort_keys=True).encode()
        ),
    )
    manager.staged(JOB, TOKEN)
    live: set[int] = set()

    def kill(pid, _signal):
        if pid in live:
            return None
        raise ProcessLookupError(pid)

    monkeypatch.setattr(storage.os, "kill", kill)
    monkeypatch.setattr(storage.os, "killpg", kill)
    probed: list[Path] = []
    monkeypatch.setattr(storage, "idle", lambda observed: probed.append(observed))
    # The source still reads as the identity the run sealed, unless a test says otherwise.
    monkeypatch.setattr(
        storage, "identity", lambda command: "41183a3daba92aec6dae4989b01100dc9d5ea5db"
    )
    owner_pid = json.loads((state / "reservations.json").read_text())["jobs"][JOB]["owner_pid"]
    return {"state": state, "root": root, "live": live, "probed": probed, "owner_pid": owner_pid}


def _ledger(case) -> dict:
    return json.loads((case["state"] / "reservations.json").read_text())["jobs"]


def test_sweep_reports_then_releases_the_interrupted_reservation_and_retires_its_root(interrupted):
    report = storage.release_interrupted(interrupted["state"], apply=False)
    assert report["staged"] == 1 and report["charged_bytes"] == 1
    assert [r["job"] for r in report["releasable"]] == [JOB] and report["held"] == []
    assert (
        JOB in _ledger(interrupted) and interrupted["root"].is_dir()
    ), "a dry sweep changes nothing"

    report = storage.release_interrupted(interrupted["state"], apply=True)
    assert [r["job"] for r in report["released"]] == [JOB] and report["held"] == []
    assert JOB not in _ledger(interrupted)
    assert not interrupted["root"].exists()
    receipt = json.loads((interrupted["state"] / "receipts" / f"{JOB}.json").read_text())
    assert receipt["status"] == "released" and receipt["verified"] is False
    assert receipt["source_check"] == "unchanged" and receipt["root_retained"] is False
    assert receipt["calibration_updated"] is False
    assert receipt["terminal_owned_footprint"]["entries"] >= 3
    assert (
        interrupted["root"] in interrupted["probed"]
    ), "the independent ps/lsof probe ran on the root"
    again = storage.release_interrupted(interrupted["state"], apply=True)
    assert again == {"staged": 0, "charged_bytes": 0, "released": [], "releasable": [], "held": []}


def test_single_job_apply_requires_the_exact_plan(interrupted):
    plan = storage.plan_interrupted_release(interrupted["state"], JOB)
    assert plan["action"] == "retire_root_finish_terminal"
    with pytest.raises(storage.StorageBlocked, match="plan changed"):
        storage.apply_interrupted_release(interrupted["state"], JOB, expected_plan_sha256="0" * 64)
    assert JOB in _ledger(interrupted)
    out = storage.apply_interrupted_release(
        interrupted["state"], JOB, expected_plan_sha256=plan["sha256"]
    )
    assert out["status"] == "released" and JOB not in _ledger(interrupted)


@pytest.mark.parametrize("who", ["owner", "child", "parent_pid", "parent_pgid"])
def test_any_live_process_in_the_recorded_tree_holds_the_reservation(interrupted, who):
    interrupted["live"].add(
        {
            "owner": interrupted["owner_pid"],
            "child": 29298,
            "parent_pid": 29243,
            "parent_pgid": 29242,
        }[who]
    )
    report = storage.release_interrupted(interrupted["state"], apply=True)
    assert report["released"] == [] and report["held"][0]["job"] == JOB
    assert "still live" in report["held"][0]["reason"]
    assert JOB in _ledger(interrupted) and interrupted["root"].is_dir()


def test_a_busy_or_blind_probe_holds_the_reservation(interrupted, monkeypatch):
    def busy(_root):
        raise storage.StorageBlocked("open file in owned storage")

    monkeypatch.setattr(storage, "idle", busy)
    plan_only = storage.release_interrupted(interrupted["state"], apply=False)
    assert plan_only["releasable"] == [] and plan_only["held"] == [
        {"job": JOB, "reason": "open file in owned storage"}
    ]
    report = storage.release_interrupted(interrupted["state"], apply=True)
    assert report["held"] == [{"job": JOB, "reason": "open file in owned storage"}]
    assert interrupted["root"].is_dir()


def test_a_run_that_reached_completion_is_not_released_here(interrupted):
    receipts = interrupted["state"] / "receipts"
    receipts.mkdir(mode=0o700)
    _write(receipts / f"{JOB}.json", {"status": "terminal_evidence_saved"})
    report = storage.release_interrupted(interrupted["state"], apply=True)
    assert "receipt exists" in report["held"][0]["reason"] and JOB in _ledger(interrupted)


def test_only_staged_rows_are_candidates(interrupted):
    path = interrupted["state"] / "reservations.json"
    ledger = json.loads(path.read_text())
    ledger["jobs"][JOB]["status"] = "uncertain"
    _write(path, ledger)
    assert storage.release_interrupted(interrupted["state"], apply=True)["staged"] == 0
    with pytest.raises(storage.StorageBlocked, match="only a staged reservation"):
        storage.plan_interrupted_release(interrupted["state"], JOB)


def test_a_marker_that_does_not_bind_the_row_holds_it(interrupted):
    _write(interrupted["root"] / "owner.json", {"job": JOB, "token": "someone-else"})
    report = storage.release_interrupted(interrupted["state"], apply=True)
    assert "does not bind" in report["held"][0]["reason"] and interrupted["root"].is_dir()


def test_a_prior_retirement_attempt_is_not_replayed(interrupted):
    interrupted["root"].with_name(interrupted["root"].name + ".retiring").mkdir()
    plan_only = storage.release_interrupted(interrupted["state"], apply=False)
    assert (
        plan_only["releasable"] == []
        and "existing retirement attempt" in plan_only["held"][0]["reason"]
    )
    report = storage.release_interrupted(interrupted["state"], apply=True)
    assert "existing retirement attempt" in report["held"][0]["reason"]


def test_a_root_already_gone_releases_the_charge_without_deleting_anything(interrupted):
    import shutil

    shutil.rmtree(interrupted["root"])
    report = storage.release_interrupted(interrupted["state"], apply=True)
    assert report["released"][0]["status"] == "released_root_absent"
    assert JOB not in _ledger(interrupted)


def test_no_ledger_is_an_empty_report(tmp_path):
    assert storage.release_interrupted(tmp_path / "missing", apply=True) == {
        "staged": 0,
        "charged_bytes": 0,
        "released": [],
        "releasable": [],
        "held": [],
    }


@pytest.mark.parametrize(
    "identity, check",
    [
        (lambda command: "0" * 40, "changed"),
        (
            lambda command: (_ for _ in ()).throw(subprocess.CalledProcessError(1, command)),
            "uninspectable",
        ),
        (
            lambda command: (_ for _ in ()).throw(subprocess.TimeoutExpired(command, 30)),
            "uninspectable",
        ),
    ],
)
def test_a_changed_or_unreadable_source_releases_the_charge_but_keeps_the_root(
    interrupted, monkeypatch, identity, check
):
    monkeypatch.setattr(storage, "identity", identity)
    plan = storage.plan_interrupted_release(interrupted["state"], JOB)
    assert plan["action"] == "finish_terminal_retain_root" and plan["source_check"] == check
    report = storage.release_interrupted(interrupted["state"], apply=True)
    assert report["released"][0]["status"] == "released_root_retained"
    assert JOB not in _ledger(interrupted)
    assert (interrupted["root"] / "staging" / "bulk.bin").exists(), "the evidence stays"
    receipt = json.loads((interrupted["state"] / "receipts" / f"{JOB}.json").read_text())
    assert receipt["root_retained"] is True and receipt["source_check"] == check


def test_lock_contention_holds_the_row_and_the_next_sweep_finishes_it(interrupted, monkeypatch):
    real_finish = storage.Reservations.finish
    calls = []

    def contended(self, job, receipt, **kw):
        calls.append(kw.get("deadline"))
        if len(calls) == 1:
            raise storage.StorageLockWait("ledger lock busy")
        return real_finish(self, job, receipt, **kw)

    monkeypatch.setattr(storage.Reservations, "finish", contended)
    first = storage.release_interrupted(interrupted["state"], apply=True)
    assert first["released"] == [] and first["held"][0]["job"] == JOB
    assert "storage_lock_wait" in first["held"][0]["reason"]
    assert calls[0] is not None, "finish is given a finite lock deadline"
    assert JOB in _ledger(interrupted) and not interrupted["root"].exists()
    second = storage.release_interrupted(interrupted["state"], apply=True)
    assert second["released"][0]["status"] == "released_root_absent" and JOB not in _ledger(
        interrupted
    )


def test_our_own_interrupted_retirement_is_completed_not_replayed(interrupted):
    quarantine = interrupted["root"].with_name(interrupted["root"].name + ".retiring")
    interrupted["root"].rename(quarantine)
    plan = storage.plan_interrupted_release(interrupted["state"], JOB)
    assert plan["action"] == "finish_terminal_after_prior_retirement"
    report = storage.release_interrupted(interrupted["state"], apply=True)
    assert report["released"][0]["status"] == "released_after_prior_retirement"
    assert quarantine.exists(), "the earlier attempt's remnants are not touched"


def test_a_corrupt_ledger_is_an_error_not_an_empty_report(interrupted):
    _write_text = interrupted["state"] / "reservations.json"
    _write_text.write_text("{not json")
    with pytest.raises(storage.StorageBlocked):
        storage.release_interrupted(interrupted["state"], apply=False)


def test_a_failing_probe_holds_that_job_and_the_sweep_continues(interrupted, monkeypatch):
    def broken(_root):
        raise subprocess.CalledProcessError(1, ["ps"])

    monkeypatch.setattr(storage, "idle", broken)
    report = storage.release_interrupted(interrupted["state"], apply=True)
    assert report["released"] == [] and report["held"][0]["job"] == JOB


def test_a_quarantine_that_is_not_ours_is_never_completed(interrupted):
    quarantine = interrupted["root"].with_name(interrupted["root"].name + ".retiring")
    interrupted["root"].rename(quarantine)
    _write(quarantine / "owner.json", {"job": JOB, "token": "someone-else"})
    report = storage.release_interrupted(interrupted["state"], apply=True)
    assert report["released"] == [] and "existing retirement attempt" in report["held"][0]["reason"]
    assert JOB in _ledger(interrupted)
