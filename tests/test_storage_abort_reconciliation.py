"""The abort reconciler only releases a proven-dead, pre-install reservation."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import shlex
import sys
from pathlib import Path

import pytest

import storage_lifecycle as storage


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, sort_keys=True))
    path.chmod(0o600)


def _rebind_proof(case: dict) -> dict:
    request = json.loads(case["request"].read_text())
    request["abort_proof"]["sha256"] = _sha(case["proof"])
    _write(case["request"], request)
    return request


def _rewrite_stage_binding(case: dict, mutate) -> None:
    path = storage._stage_source_binding_path(case["state"], case["job"])
    binding = json.loads(path.read_text())
    mutate(binding)
    body = {key: value for key, value in binding.items() if key != "sha256"}
    binding["sha256"] = storage._json_digest(body)
    _write(path, binding)
    ledger_path = case["state"] / "reservations.json"
    ledger = json.loads(ledger_path.read_text())
    ledger["jobs"][case["job"]]["stage_source_binding_sha256"] = _sha(path)
    _write(ledger_path, ledger)
    request = json.loads(case["request"].read_text())
    request["expected_row_sha256"] = storage._json_digest(ledger["jobs"][case["job"]])
    _write(case["request"], request)


def _synchronize_sealed_parent(case: dict, parent: dict) -> None:
    """Update all authenticated fixture references except the check under test."""
    _rewrite_stage_binding(case, lambda binding: binding.update(release_parent=parent))
    proof = json.loads(case["proof"].read_text())
    proof["processes"][0] = {key: parent[key] for key in ("pid", "ppid", "pgid", "command")}
    proof["processes"][1]["ppid"] = parent["pid"]
    proof["processes"][1]["pgid"] = parent["pgid"]
    _write(case["proof"], proof)
    request = _rebind_proof(case)
    request["release_owner_process_sha256"] = parent["command_sha256"]
    _write(case["request"], request)


def _parent(case: dict) -> dict:
    return json.loads(storage._stage_source_binding_path(case["state"], case["job"]).read_text())[
        "release_parent"
    ]


def _hold_owner_lock(path: str, ready, release) -> None:
    import fcntl

    descriptor = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        ready.set()
        release.wait(10)
    finally:
        os.close(descriptor)


@pytest.fixture
def aborted(tmp_path, monkeypatch):
    state = tmp_path / "state"
    root = tmp_path / "owned-root"
    root.mkdir(mode=0o700)
    token = "owner-token"
    job = "c1b375d724024492af362689382f2ca0"
    candidate = "c6133aa7bd9f06fe299ef7cadc66b35440eaae71"
    failed = tmp_path / "release-final.json"
    release_owner_command = (
        "/opt/anaconda3/bin/python3 -B scripts/closer_verified_release.py install "
        + f"--expected-source-sha {candidate} --journal {failed}"
    )
    source_root = tmp_path / "source"
    source_root.mkdir()
    verifier = source_root / "verify_before_sync.sh"
    verifier.write_text("#!/bin/bash\n")
    source_command = ["bash", str(verifier), "--identity", str(source_root)]
    child_command = [
        "bash",
        str(verifier),
        "--snapshot-out",
        str(tmp_path / "snapshot"),
        "--digest-out",
        str(tmp_path / "digest"),
        str(source_root),
    ]
    _write(root / "owner.json", {"job": job, "token": token})
    manager = storage.Reservations(state, tmp_path, floor=0, free=lambda _: 10**9)
    manager.admit(job, "verify", 1, 1, owner_token=token, scope="one")
    manager.bind(
        job,
        root=str(root),
        root_identity=list((root.stat().st_dev, root.stat().st_ino)),
        source_identity="1e32a1ef506bef4581615d5d53ef9552dcd3cffe",
        source_command=source_command,
        child_pgid=74260,
    )
    (state / f"{job}.owner").touch(mode=0o600)
    row = json.loads((state / "reservations.json").read_text())["jobs"][job]
    binding = storage._stage_source_binding(
        job=job,
        row=row,
        child_command=child_command,
        release_parent={
            "pid": 7000,
            "ppid": 1,
            "pgid": 7000,
            "command": release_owner_command,
            "argv": shlex.split(release_owner_command),
            "command_sha256": hashlib.sha256(release_owner_command.encode()).hexdigest(),
            "candidate_sha": candidate,
            "journal": str(failed),
        },
    )
    storage._create_private_json_once(storage._stage_source_binding_path(state, job), binding)
    manager.bind(
        job,
        stage_source_binding_sha256=storage._sha256_bytes(
            json.dumps(binding, sort_keys=True).encode()
        ),
    )
    manager.staged(job, token)
    row = json.loads((state / "reservations.json").read_text())["jobs"][job]
    proof = tmp_path / "abort.json"
    owner_command = (
        f"/opt/anaconda3/bin/python3 {tmp_path}/storage_lifecycle.py run " f"--source {source_root}"
    )
    child_proof_command = " ".join(child_command)
    _write(
        proof,
        {
            "candidate": candidate,
            "signal": "SIGTERM",
            "processes": [
                {
                    "pid": 7000,
                    "ppid": 1,
                    "pgid": 7000,
                    "command": release_owner_command,
                },
                {
                    "pid": row["owner_pid"],
                    "ppid": 7000,
                    "pgid": 7000,
                    "command": owner_command,
                },
                {
                    "pid": 74260,
                    "ppid": row["owner_pid"],
                    "pgid": 74260,
                    "command": child_proof_command,
                },
            ],
        },
    )
    _write(
        failed,
        {
            "candidate_sha": candidate,
            "incumbent_token": "generation-v1:incumbent",
            "installed_generation": None,
            "stage": "failed",
            "failure": {"stage": "verification", "exit_code": 143},
            "verification": {"verified": False, "exit_code": 143, "log_path": str(proof)},
        },
    )
    request = tmp_path / "request.json"
    _write(
        request,
        {
            "schema": storage.ABORT_RECONCILIATION_SCHEMA,
            "caller": "original-human-task-reconciler",
            "state_dir": str(state),
            "job": job,
            "expected_row_sha256": storage._json_digest(row),
            "abort_proof": {"path": str(proof), "sha256": _sha(proof)},
            "failed_release": {"path": str(failed), "sha256": _sha(failed)},
            "candidate_sha": candidate,
            "incumbent_token": "generation-v1:incumbent",
            "owner_process_sha256": hashlib.sha256(owner_command.encode()).hexdigest(),
            "child_process_sha256": hashlib.sha256(child_proof_command.encode()).hexdigest(),
            "release_owner_process_sha256": hashlib.sha256(
                release_owner_command.encode()
            ).hexdigest(),
        },
    )

    def dead(pid, _signal):
        raise ProcessLookupError(pid)

    monkeypatch.setattr(storage.os, "kill", dead)
    monkeypatch.setattr(storage.os, "killpg", dead)
    monkeypatch.setattr(
        storage,
        "idle",
        lambda observed: observed == root or (_ for _ in ()).throw(AssertionError()),
    )
    return {"state": state, "root": root, "job": job, "request": request, "proof": proof}


def test_plan_then_apply_releases_only_ledger_reservation_and_retains_root(aborted):
    plan = storage.plan_abort_reconciliation(aborted["request"])
    known = storage.apply_abort_reconciliation(
        aborted["request"], expected_plan_sha256=plan["sha256"]
    )
    assert known["root_retained"] is True
    assert aborted["root"].is_dir()
    ledger = json.loads((aborted["state"] / "reservations.json").read_text())
    assert aborted["job"] not in ledger["jobs"]
    receipt = json.loads((aborted["state"] / "receipts" / f"{aborted['job']}.json").read_text())
    assert receipt["status"] == "verification_aborted_pre_install_reconciled"
    assert receipt["exit_code"] == 143 and receipt["verified"] is False
    assert receipt["root_retained"] is True and receipt["physical_reclaim_claimed"] is False
    with pytest.raises(storage.StorageBlocked, match="prior reconciliation evidence"):
        storage.plan_abort_reconciliation(aborted["request"])


@pytest.mark.parametrize(
    "mutator, message",
    [
        (lambda case: case["proof"].write_text("{}"), "abort proof reference changed"),
        (
            lambda case: (case["root"] / "owner.json").write_text(
                '{"job":"wrong","token":"owner-token"}'
            ),
            "owned root marker",
        ),
    ],
)
def test_plan_refuses_changed_evidence(aborted, mutator, message):
    mutator(aborted)
    with pytest.raises(storage.StorageBlocked, match=message):
        storage.plan_abort_reconciliation(aborted["request"])


def test_plan_refuses_changed_row(aborted):
    ledger_path = aborted["state"] / "reservations.json"
    ledger = json.loads(ledger_path.read_text())
    ledger["jobs"][aborted["job"]]["status"] = "uncertain"
    _write(ledger_path, ledger)
    with pytest.raises(storage.StorageBlocked, match="reservation changed"):
        storage.plan_abort_reconciliation(aborted["request"])


def test_plan_refuses_missing_or_mismatched_stage_source_binding(aborted):
    path = storage._stage_source_binding_path(aborted["state"], aborted["job"])
    path.unlink()
    with pytest.raises(storage.StorageBlocked, match="stage/source binding"):
        storage.plan_abort_reconciliation(aborted["request"])


def test_plan_refuses_legacy_row_without_stage_binding_digest(aborted):
    ledger_path = aborted["state"] / "reservations.json"
    ledger = json.loads(ledger_path.read_text())
    row = ledger["jobs"][aborted["job"]]
    del row["stage_source_binding_sha256"]
    _write(ledger_path, ledger)
    request = json.loads(aborted["request"].read_text())
    request["expected_row_sha256"] = storage._json_digest(row)
    _write(aborted["request"], request)
    with pytest.raises(storage.StorageBlocked, match="stage/source binding is missing"):
        storage.plan_abort_reconciliation(aborted["request"])


def test_plan_refuses_stage_binding_that_no_longer_matches_row(aborted):
    _rewrite_stage_binding(
        aborted,
        lambda binding: binding.update(source_command=["bash", "/wrong", "--identity", "/wrong"]),
    )
    with pytest.raises(storage.StorageBlocked, match="stage/source binding differs"):
        storage.plan_abort_reconciliation(aborted["request"])


def test_plan_refuses_legacy_stage_binding_without_parent_release_provenance(aborted):
    _rewrite_stage_binding(aborted, lambda binding: binding.pop("release_parent"))
    with pytest.raises(storage.StorageBlocked, match="stage/source binding differs"):
        storage.plan_abort_reconciliation(aborted["request"])


def test_plan_refuses_abort_parent_pid_reuse(aborted):
    proof = json.loads(aborted["proof"].read_text())
    proof["processes"][0]["pid"] += 1
    proof["processes"][0]["pgid"] += 1
    proof["processes"][1]["ppid"] += 1
    proof["processes"][1]["pgid"] += 1
    _write(aborted["proof"], proof)
    _rebind_proof(aborted)
    with pytest.raises(storage.StorageBlocked, match="recorded verifier process tree"):
        storage.plan_abort_reconciliation(aborted["request"])


def test_plan_refuses_abort_parent_command_different_from_sealed_stage_parent(aborted):
    proof = json.loads(aborted["proof"].read_text())
    proof["processes"][0]["command"] += " --unexpected"
    _write(aborted["proof"], proof)
    request = _rebind_proof(aborted)
    request["release_owner_process_sha256"] = hashlib.sha256(
        proof["processes"][0]["command"].encode()
    ).hexdigest()
    _write(aborted["request"], request)
    with pytest.raises(storage.StorageBlocked, match="recorded verifier process tree"):
        storage.plan_abort_reconciliation(aborted["request"])


def test_plan_refuses_foreign_failed_journal_even_with_matching_parent_tree(aborted):
    parent = _parent(aborted)
    foreign = aborted["state"] / "foreign-release.json"
    parent["argv"][parent["argv"].index("--journal") + 1] = str(foreign)
    parent["journal"] = str(foreign)
    parent["command"] = " ".join(shlex.quote(value) for value in parent["argv"])
    parent["command_sha256"] = hashlib.sha256(parent["command"].encode()).hexdigest()
    _synchronize_sealed_parent(aborted, parent)
    with pytest.raises(storage.StorageBlocked, match="sealed release provenance"):
        storage.plan_abort_reconciliation(aborted["request"])


def test_stage_binding_refuses_candidate_outside_exact_typed_flag(aborted):
    row = json.loads((aborted["state"] / "reservations.json").read_text())["jobs"][aborted["job"]]
    parent = _parent(aborted)
    index = parent["argv"].index("--expected-source-sha") + 1
    parent["argv"][index] = "0" * 40
    parent["argv"].append(parent["candidate_sha"])
    parent["command"] = " ".join(shlex.quote(value) for value in parent["argv"])
    parent["command_sha256"] = hashlib.sha256(parent["command"].encode()).hexdigest()
    with pytest.raises(storage.StorageBlocked, match="binding inputs differ"):
        storage._stage_source_binding(
            job=aborted["job"],
            row=row,
            child_command=json.loads(
                storage._stage_source_binding_path(aborted["state"], aborted["job"]).read_text()
            )["child_command"],
            release_parent=parent,
        )


def test_release_parent_snapshot_seals_exact_release_argv(monkeypatch, tmp_path):
    candidate = "a" * 40
    journal = tmp_path / "release.json"
    command = (
        "/opt/anaconda3/bin/python3 -B scripts/closer_verified_release.py install "
        f"--expected-source-sha {candidate} --journal {journal}"
    )

    class Process:
        args = ["ps"]
        returncode = 0

        def communicate(self, timeout):
            assert timeout == 10
            return f"701 1 701 {command}\n", ""

    monkeypatch.setattr(storage.os, "getppid", lambda: 701)
    monkeypatch.setattr(storage, "_SYSTEM_POPEN", lambda *_args, **_kwargs: Process())
    assert storage._release_parent_snapshot() == {
        "pid": 701,
        "ppid": 1,
        "pgid": 701,
        "command": command,
        "argv": shlex.split(command),
        "command_sha256": hashlib.sha256(command.encode()).hexdigest(),
        "candidate_sha": candidate,
        "journal": str(journal),
    }


def test_release_parent_snapshot_refuses_mismatched_ps_pid(monkeypatch):
    class Process:
        args = ["ps"]
        returncode = 0

        def communicate(self, timeout):
            return "702 1 702 release\n", ""

    monkeypatch.setattr(storage.os, "getppid", lambda: 701)
    monkeypatch.setattr(storage, "_SYSTEM_POPEN", lambda *_args, **_kwargs: Process())
    with pytest.raises(storage.StorageBlocked, match="snapshot identity changed"):
        storage._release_parent_snapshot()


def test_plan_refuses_live_pid(aborted, monkeypatch):
    monkeypatch.setattr(storage.os, "kill", lambda _pid, _signal: None)
    with pytest.raises(storage.StorageBlocked, match="still live"):
        storage.plan_abort_reconciliation(aborted["request"])


def test_plan_refuses_live_process_group(aborted, monkeypatch):
    monkeypatch.setattr(storage.os, "killpg", lambda _pgid, _signal: None)
    with pytest.raises(storage.StorageBlocked, match="still live"):
        storage.plan_abort_reconciliation(aborted["request"])


@pytest.mark.parametrize(
    "mutate",
    [
        lambda processes: processes[1].update(ppid=9999),
        lambda processes: processes[1].update(pgid=9999),
        lambda processes: processes[2].update(ppid=9999),
        lambda processes: processes.append(dict(processes[2])),
    ],
)
def test_plan_refuses_unjoined_or_duplicate_abort_process_records(aborted, mutate):
    proof = json.loads(aborted["proof"].read_text())
    mutate(proof["processes"])
    _write(aborted["proof"], proof)
    _rebind_proof(aborted)
    with pytest.raises(storage.StorageBlocked, match="process tree|duplicate"):
        storage.plan_abort_reconciliation(aborted["request"])


def test_plan_refuses_abort_proof_with_different_child_command(aborted):
    proof = json.loads(aborted["proof"].read_text())
    proof["processes"][2]["command"] += " --unexpected"
    _write(aborted["proof"], proof)
    _rebind_proof(aborted)
    request = json.loads(aborted["request"].read_text())
    request["child_process_sha256"] = hashlib.sha256(
        proof["processes"][2]["command"].encode()
    ).hexdigest()
    _write(aborted["request"], request)
    with pytest.raises(storage.StorageBlocked, match="source command differs"):
        storage.plan_abort_reconciliation(aborted["request"])


def test_plan_refuses_abort_proof_with_different_owner_source(aborted):
    proof = json.loads(aborted["proof"].read_text())
    proof["processes"][1]["command"] = proof["processes"][1]["command"].replace(
        "--source ", "--source /wrong "
    )
    _write(aborted["proof"], proof)
    _rebind_proof(aborted)
    request = json.loads(aborted["request"].read_text())
    request["owner_process_sha256"] = hashlib.sha256(
        proof["processes"][1]["command"].encode()
    ).hexdigest()
    _write(aborted["request"], request)
    with pytest.raises(storage.StorageBlocked, match="source command differs"):
        storage.plan_abort_reconciliation(aborted["request"])


@pytest.mark.parametrize("candidate", ["A" * 40, "g" * 40, "a" * 39, "a" * 41])
def test_plan_refuses_noncanonical_candidate_sha(aborted, candidate):
    request = json.loads(aborted["request"].read_text())
    request["candidate_sha"] = candidate
    _write(aborted["request"], request)
    with pytest.raises(storage.StorageBlocked, match="invalid reconciliation request"):
        storage.plan_abort_reconciliation(aborted["request"])


def test_plan_refuses_blind_or_busy_probe(aborted, monkeypatch):
    monkeypatch.setattr(
        storage,
        "idle",
        lambda _root: (_ for _ in ()).throw(
            storage.StorageBlocked("open-file inspection is blind to supervisor")
        ),
    )
    with pytest.raises(storage.StorageBlocked, match="blind"):
        storage.plan_abort_reconciliation(aborted["request"])


def test_apply_refuses_row_race_after_plan(aborted):
    plan = storage.plan_abort_reconciliation(aborted["request"])
    ledger_path = aborted["state"] / "reservations.json"
    ledger = json.loads(ledger_path.read_text())
    ledger["jobs"][aborted["job"]]["remaining"] = 99
    _write(ledger_path, ledger)
    with pytest.raises(storage.StorageBlocked, match="reservation changed"):
        storage.apply_abort_reconciliation(aborted["request"], expected_plan_sha256=plan["sha256"])
    assert not (
        aborted["state"] / "abort-reconciliations" / f"{aborted['job']}.intent.json"
    ).exists()


def test_apply_requires_exact_plan_digest(aborted):
    with pytest.raises(storage.StorageBlocked, match="plan changed"):
        storage.apply_abort_reconciliation(aborted["request"], expected_plan_sha256="0" * 64)


def test_apply_accepts_existing_private_reconciliation_namespace(aborted):
    evidence = aborted["state"] / "abort-reconciliations"
    evidence.mkdir(mode=0o700)
    plan = storage.plan_abort_reconciliation(aborted["request"])
    known = storage.apply_abort_reconciliation(
        aborted["request"], expected_plan_sha256=plan["sha256"]
    )
    assert Path(known["receipt"]["path"]).is_file()


def test_reconciliation_does_not_update_existing_calibration(aborted):
    ledger_path = aborted["state"] / "reservations.json"
    ledger = json.loads(ledger_path.read_text())
    contract = {"sha256": "a" * 64}
    calibration = {
        "contract": contract,
        "allocated_highwater_bytes": 123,
        "access_sequence": 1,
    }
    ledger["owned_measurements"] = {contract["sha256"]: calibration}
    ledger["owned_cache_sequence"] = 1
    _write(ledger_path, ledger)
    plan = storage.plan_abort_reconciliation(aborted["request"])
    storage.apply_abort_reconciliation(aborted["request"], expected_plan_sha256=plan["sha256"])
    after = json.loads(ledger_path.read_text())
    assert after["owned_measurements"] == {contract["sha256"]: calibration}
    assert after["owned_cache_sequence"] == 1


def test_request_and_reference_are_single_descriptor_snapshots(aborted, monkeypatch):
    request_raw = aborted["request"].read_bytes()
    replacement = aborted["request"].with_name("replacement-request.json")
    changed = json.loads(request_raw)
    changed["job"] = "otherjob"
    _write(replacement, changed)
    original_read = storage.os.read
    replaced = False

    def replace_after_read(descriptor, amount):
        nonlocal replaced
        raw = original_read(descriptor, amount)
        if raw and not replaced:
            replaced = True
            os.replace(replacement, aborted["request"])
        return raw

    monkeypatch.setattr(storage.os, "read", replace_after_read)
    plan = storage.plan_abort_reconciliation(aborted["request"])
    assert plan["request_path"] == str(Path(os.path.abspath(aborted["request"])))
    assert plan["job"] == aborted["job"]
    assert plan["request_sha256"] == hashlib.sha256(request_raw).hexdigest()


def test_reference_payload_is_hashed_and_parsed_from_same_snapshot(aborted, monkeypatch):
    request = json.loads(aborted["request"].read_text())
    raw = aborted["proof"].read_bytes()
    replacement = aborted["proof"].with_name("replacement-proof.json")
    changed = json.loads(raw)
    changed["candidate"] = "0" * 40
    _write(replacement, changed)
    original_read = storage.os.read
    replaced = False

    def replace_after_read(descriptor, amount):
        nonlocal replaced
        chunk = original_read(descriptor, amount)
        if chunk and not replaced:
            replaced = True
            os.replace(replacement, aborted["proof"])
        return chunk

    monkeypatch.setattr(storage.os, "read", replace_after_read)
    _, proof = storage._read_json_reference(request["abort_proof"], label="abort proof")
    assert proof["candidate"] != "0" * 40


def test_cli_plan_and_apply_emit_sorted_json(aborted, monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        ["storage_lifecycle.py", "reconcile-abort", "--request", str(aborted["request"])],
    )
    assert storage.main() == 0
    plan_text = capsys.readouterr().out
    plan = json.loads(plan_text)
    assert plan_text == json.dumps(plan, sort_keys=True) + "\n"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "storage_lifecycle.py",
            "reconcile-abort",
            "--request",
            str(aborted["request"]),
            "--apply",
            "--expected-plan-sha256",
            plan["sha256"],
        ],
    )
    assert storage.main() == 0
    known_text = capsys.readouterr().out
    assert known_text == json.dumps(json.loads(known_text), sort_keys=True) + "\n"


@pytest.mark.parametrize(
    "argv",
    [
        ["storage_lifecycle.py", "reconcile-abort", "--request", "request.json", "--apply"],
        [
            "storage_lifecycle.py",
            "reconcile-abort",
            "--request",
            "request.json",
            "--expected-plan-sha256",
            "0" * 64,
        ],
    ],
)
def test_cli_rejects_incomplete_or_mismatched_apply_arguments(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit, match="2"):
        storage.main()


def test_plan_refuses_held_owner_lock(aborted):
    path = aborted["state"] / f"{aborted['job']}.owner"
    context = multiprocessing.get_context("fork")
    ready, release = context.Event(), context.Event()
    holder = context.Process(target=_hold_owner_lock, args=(str(path), ready, release))
    holder.start()
    try:
        with pytest.raises(storage.StorageBlocked, match="still active"):
            assert ready.wait(5)
            storage.plan_abort_reconciliation(aborted["request"])
    finally:
        release.set()
        holder.join(10)
        assert holder.exitcode == 0
