from __future__ import annotations

import json
import subprocess
import threading
import time
import venv
from pathlib import Path

import pytest

from closer_release import ReleaseConflict, ReleaseError, ReleaseHeld, ReleaseRecord

CANDIDATE = "a" * 40
INCUMBENT = "generation-v1:" + "b" * 64
DIGEST = "c" * 64
GENERATION = "generation-v1:" + "d" * 64


@pytest.fixture(autouse=True)
def private_release_home(tmp_path, monkeypatch):
    """Release tests must never inspect or create the user's live Closer lane."""
    monkeypatch.setenv("HOME", str(tmp_path / "private-home"))


def start_verified(record: ReleaseRecord, tmp_path):
    state = record.begin(CANDIDATE, INCUMBENT, "closer", run_id="run-1")
    verifying = record.transition(
        "verifying", lane_owner="closer", expected_revision=state["revision"]
    )
    return record.record_verification(
        lane_owner="closer",
        expected_revision=verifying["revision"],
        verified=True,
        source_identity="e" * 64,
        snapshot_path=tmp_path / "snapshot",
        snapshot_digest=DIGEST,
        log_path=tmp_path / "verify.log",
        exit_code=0,
    )


def wait_for_path(path: Path, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        threading.Event().wait(0.01)
    return path.exists()


def test_record_survives_restart_and_preserves_original_incumbent_token(tmp_path):
    path = tmp_path / "release.json"
    first = ReleaseRecord(path)
    initial = first.begin(CANDIDATE, INCUMBENT, "closer", run_id="run-1")
    verifying = first.transition(
        "verifying", lane_owner="closer", expected_revision=initial["revision"]
    )
    verified = first.record_verification(
        lane_owner="closer",
        expected_revision=verifying["revision"],
        verified=True,
        source_identity="e" * 64,
        snapshot_path=tmp_path / "verified",
        snapshot_digest=DIGEST,
        log_path=tmp_path / "verify.log",
        exit_code=0,
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
    verifying = record.transition(
        "verifying", lane_owner="closer", expected_revision=initial["revision"]
    )
    failed = record.record_verification(
        lane_owner="closer",
        expected_revision=verifying["revision"],
        verified=False,
        source_identity=None,
        snapshot_path=None,
        snapshot_digest=None,
        log_path=tmp_path / "verify.log",
        exit_code=1,
    )
    assert failed["stage"] == "failed"
    assert ReleaseRecord(record.path).read()["failure"] == {"stage": "verification", "exit_code": 1}
    with pytest.raises(ReleaseError, match="illegal release transition"):
        record.transition("installing", lane_owner="closer", expected_revision=failed["revision"])


def test_install_acceptance_rollback_stages_and_revision_cas(tmp_path):
    record = ReleaseRecord(tmp_path / "release.json")
    verified = start_verified(record, tmp_path)
    installing = record.transition(
        "installing", lane_owner="closer", expected_revision=verified["revision"]
    )
    installed = record.transition(
        "installed",
        lane_owner="closer",
        expected_revision=installing["revision"],
        installed_generation=GENERATION,
    )
    pending = record.transition(
        "acceptance_pending", lane_owner="closer", expected_revision=installed["revision"]
    )
    accepted = record.transition(
        "accepted",
        lane_owner="closer",
        expected_revision=pending["revision"],
        live_acceptance={"accepted": True, "evidence": "health check passed"},
    )
    assert accepted["stage"] == "accepted"
    with pytest.raises(ReleaseConflict, match="revision changed"):
        record.transition(
            "rollback_pending", lane_owner="closer", expected_revision=installed["revision"]
        )

    rollback_record = ReleaseRecord(tmp_path / "rollback.json")
    verified2 = start_verified(rollback_record, tmp_path)
    installing2 = rollback_record.transition(
        "installing", lane_owner="closer", expected_revision=verified2["revision"]
    )
    installed2 = rollback_record.transition(
        "installed",
        lane_owner="closer",
        expected_revision=installing2["revision"],
        installed_generation=GENERATION,
    )
    rollback = rollback_record.transition(
        "rollback_pending",
        lane_owner="closer",
        expected_revision=installed2["revision"],
        rollback={"reason": "acceptance failed", "restore_token": INCUMBENT},
    )
    done = rollback_record.transition(
        "rolled_back", lane_owner="closer", expected_revision=rollback["revision"]
    )
    assert done["rollback"]["restore_token"] == INCUMBENT


def test_record_is_bounded_and_atomic_json(tmp_path):
    record = ReleaseRecord(tmp_path / "release.json")
    record.begin(CANDIDATE, INCUMBENT, "closer")
    assert json.loads(record.path.read_text())["schema_version"] == 1
    assert not list(tmp_path.glob(".release.json.*.tmp"))
    record.path.write_text("x" * (64 * 1024 + 1))
    with pytest.raises(ReleaseError, match="size bound"):
        record.read()


VERIFIER_SOURCE = r"""#!/usr/bin/env bash
set -eu
mode=verify
if [[ "${1:-}" == "--identity" ]]; then mode=identity; shift; fi
if [[ -n "${VERIFIER_LOG:-}" ]]; then printf '%s\n' "$mode" >> "$VERIFIER_LOG"; fi
if [[ "$mode" == identity ]]; then source_path="$1"; [[ -d "$source_path" ]]; printf '%s\n' "$SOURCE_FINGERPRINT"; exit 0; fi
[[ -z "${VERIFY_BEFORE_SYNC_MAX_DIR_MB:-}" ]]
[[ -z "${VERIFY_BEFORE_SYNC_TREE:-}" ]]
[[ -z "${VERIFY_BEFORE_SYNC_FLOOR_MAY_LAG:-}" ]]
if [[ -n "${EXPECTED_PYTHON:-}" ]]; then [[ "$PYTHON" == "$EXPECTED_PYTHON" ]]; fi
[[ "${VERIFY_BEFORE_SYNC_OWNER_KEY:-}" == "$EXPECTED_STORAGE_OWNER" ]]
[[ "${ORCH_STORAGE_STATE_DIR:-}" == "$EXPECTED_STORAGE_LEDGER" ]]
[[ "$1" == "--snapshot-out" ]]; snapshot="$2"; shift 2
[[ "$1" == "--digest-out" ]]; digest="$2"; shift 2
source_path="$1"
[[ -d "$source_path" ]]
if [[ -f "$source_path/fail-verification" ]]; then exit 1; fi
mkdir -p "$snapshot"
printf 'candidate payload' > "$snapshot/payload"
printf '%s\n' "$(printf 'c%.0s' {1..64})" > "$digest"
if [[ -n "${PUBLISH_DURING_VERIFY:-}" ]]; then printf '%s\n' "$PUBLISH_DURING_VERIFY" > "$GENERATION_STATE"; fi
printf 'verified source identity: %s\n' "$SOURCE_FINGERPRINT"
"""


FAKE_INSTALLER = r"""import json
import os
import shutil
import sys
import time
from pathlib import Path

args = sys.argv[1:]
state = Path(os.environ["GENERATION_STATE"])
mirror = Path(args[0])

def token():
    return "generation-v1:" + state.read_text().strip()

def mark_and_wait(started_name, release_name):
    started = os.environ.get(started_name)
    if started:
        Path(started).write_text("started")
    release = os.environ.get(release_name)
    if release:
        while not Path(release).exists():
            time.sleep(0.01)

if "--generation-token" in args:
    if state.read_text().strip() == "d" * 64:
        mark_and_wait("READBACK_STARTED", "READBACK_RELEASE")
    print(token())
    raise SystemExit(0)
if "--digest" in args:
    target = mirror / "payload"
    print("c" * 64 if target.is_file() and target.read_text() == "candidate payload" else "1" * 64)
    raise SystemExit(0)
snapshot = mirror
mirror = Path(args[1])
expected = args[args.index("--expected-digest") + 1]
expected_generation = args[args.index("--expected-generation") + 1]
if token() != expected_generation:
    print("CAS mismatch", file=sys.stderr)
    raise SystemExit(2)
if (snapshot / "payload").read_text() != "candidate payload" or expected != "c" * 64:
    raise SystemExit(3)
mark_and_wait("INSTALL_STARTED", "INSTALL_RELEASE")
mirror.mkdir(parents=True, exist_ok=True)
shutil.copyfile(snapshot / "payload", mirror / "payload")
state.write_text("d" * 64)
"""


def make_fake_release(tmp_path, monkeypatch, *, extra_env=None, python=None, invoke=True):
    import sys

    source = tmp_path / "source"
    source.mkdir()
    if (extra_env or {}).get("FAIL_VERIFY"):
        (source / "fail-verification").touch()
    (source / "README.md").write_text("frozen release fixture\n")
    subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(source), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "frozen fixture",
        ],
        check=True,
    )
    candidate = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    if (extra_env or {}).get("DIRTY_SOURCE"):
        (source / "untracked.txt").write_text("not frozen\n")
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    (mirror / "payload").write_text("incumbent payload\n")
    generation_state = tmp_path / "generation.txt"
    generation_state.write_text("b" * 64)
    verifier = tmp_path / "verify.sh"
    verifier.write_text(VERIFIER_SOURCE)
    verifier.chmod(0o755)
    installer = tmp_path / "fake_installer.py"
    installer.write_text(FAKE_INSTALLER)
    journal = ReleaseRecord(tmp_path / "release.json")
    monkeypatch.setenv("GENERATION_STATE", str(generation_state))
    monkeypatch.setenv("SOURCE_FINGERPRINT", "e" * 40)
    monkeypatch.setenv(
        "EXPECTED_STORAGE_OWNER", "publication:" + str(mirror.parent.resolve() / mirror.name)
    )
    shared = (extra_env or {}).get("ORCH_STORAGE_STATE_DIR", str(tmp_path / "shared-storage"))
    monkeypatch.setenv("ORCH_STORAGE_STATE_DIR", shared)
    monkeypatch.setenv("EXPECTED_STORAGE_LEDGER", shared)
    for name, value in (extra_env or {}).items():
        monkeypatch.setenv(name, value)
    def call():
        return journal.verify_and_install(
            candidate_sha=CANDIDATE if (extra_env or {}).get("WRONG_CANDIDATE") else candidate,
            lane_owner="closer",
            source=source,
            mirror=mirror,
            verifier=verifier,
            installer=installer,
            python=python or sys.executable,
            bash="/bin/bash",
            run_id="fake-run",
        )
    if not invoke:
        return journal, mirror, generation_state, call
    result = call()
    return result, journal, mirror, generation_state


def test_verify_and_install_runs_gate_then_installs_and_reads_back_generation(
    tmp_path, monkeypatch
):
    result, journal, mirror, _state = make_fake_release(
        tmp_path,
        monkeypatch,
        extra_env={
            "VERIFY_BEFORE_SYNC_MAX_DIR_MB": "500",
            "VERIFY_BEFORE_SYNC_TREE": "BARE EXEC MIRROR",
            "VERIFY_BEFORE_SYNC_OWNER_KEY": "wrong-per-candidate-key",
        },
    )
    assert result["stage"] == "installed"
    assert result["incumbent_token"] == INCUMBENT
    assert result["installed_generation"] == GENERATION
    assert journal.read()["verification"]["snapshot_digest"] == DIGEST
    assert (mirror / "payload").read_text() == "candidate payload"


def test_verify_and_install_keeps_selected_venv_python_for_the_gate(tmp_path, monkeypatch):
    virtualenv = tmp_path / "venv"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(virtualenv)
    selected = virtualenv / "bin" / "python"
    assert selected.is_symlink()
    result, _, _, _ = make_fake_release(
        tmp_path,
        monkeypatch,
        extra_env={"EXPECTED_PYTHON": str(selected.absolute())},
        python=selected,
    )
    assert result["stage"] == "installed"


def test_storage_publication_scope_is_stable_across_generations_and_journal_owners(
    tmp_path, monkeypatch
):
    import sys

    from closer_release import _verification_env

    mirror = tmp_path / "mirror"
    one = tmp_path / "generation-one"
    two = tmp_path / "generation-two"
    one.mkdir()
    two.mkdir()
    mirror.symlink_to(one, target_is_directory=True)
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.delenv("ORCH_STORAGE_STATE_DIR", raising=False)
    monkeypatch.setenv("VERIFY_BEFORE_SYNC_OWNER_KEY", "per-candidate")
    first = _verification_env(Path(sys.executable), mirror)
    mirror.unlink()
    mirror.symlink_to(two, target_is_directory=True)
    second = _verification_env(Path(sys.executable), mirror)
    assert (
        first["VERIFY_BEFORE_SYNC_OWNER_KEY"]
        == second["VERIFY_BEFORE_SYNC_OWNER_KEY"]
        == "publication:" + str(mirror)
    )
    assert (
        first["ORCH_STORAGE_STATE_DIR"]
        == second["ORCH_STORAGE_STATE_DIR"]
        == str(tmp_path / "runtime/.storage-lifecycle")
    )


def test_verify_and_install_fails_closed_when_competing_publisher_wins(tmp_path, monkeypatch):
    result, journal, mirror, _state = make_fake_release(
        tmp_path,
        monkeypatch,
        extra_env={"PUBLISH_DURING_VERIFY": "f" * 64},
    )
    assert result["stage"] == "failed"
    assert result["incumbent_token"] == INCUMBENT
    assert result["failure"]["stage"] == "install"
    assert (mirror / "payload").read_text() == "incumbent payload\n"


def test_verify_and_install_records_failed_verdict_and_never_installs(tmp_path, monkeypatch):
    result, journal, mirror, _state = make_fake_release(
        tmp_path,
        monkeypatch,
        extra_env={"FAIL_VERIFY": "1"},
    )
    assert result["stage"] == "failed"
    assert result["verification"]["verified"] is False
    assert result["verification"]["exit_code"] == 1
    assert "verified source identity" not in Path(result["verification"]["log_path"]).read_text()
    assert (mirror / "payload").read_text() == "incumbent payload\n"


@pytest.mark.parametrize(
    ("flag", "reason"),
    [
        ("WRONG_CANDIDATE", "does not match source HEAD"),
        ("DIRTY_SOURCE", "uncommitted or untracked"),
    ],
)
def test_release_refuses_unbound_source_before_install(tmp_path, monkeypatch, flag, reason):
    with pytest.raises(ReleaseError, match=reason):
        make_fake_release(tmp_path, monkeypatch, extra_env={flag: "1"})
    assert not (tmp_path / "release.json").exists()
    assert (tmp_path / "mirror" / "payload").read_text() == "incumbent payload\n"


def test_source_held_closer_lane_preserves_verified_receipt_and_resumes_without_reverification(
    tmp_path, monkeypatch
):
    verifier_log = tmp_path / "verifier.log"
    monkeypatch.setenv("VERIFIER_LOG", str(verifier_log))
    journal, mirror, _generation, call = make_fake_release(tmp_path, monkeypatch, invoke=False)
    lane = Path.home() / ".codex/handoff/.lane-closer.lock"
    lane.mkdir(parents=True)

    with pytest.raises(ReleaseHeld, match="source installation held"):
        call()

    held = journal.read()
    assert held["stage"] == "verified"
    assert held["verification"]["verified"] is True
    assert (mirror / "payload").read_text() == "incumbent payload\n"
    verifier_calls = verifier_log.read_text().splitlines()
    assert verifier_calls == ["identity", "verify", "identity"]

    # An interrupted install remains uncertain.  A subsequently-held native
    # lane must defer it as-is rather than manufacture an install failure.
    journal.transition(
        "installing", lane_owner="closer", expected_revision=held["revision"]
    )
    with pytest.raises(ReleaseHeld, match="source installation held"):
        call()
    assert journal.read()["stage"] == "installing"
    assert journal.read()["failure"] is None

    lane.rmdir()
    resumed = call()
    assert resumed["stage"] == "installed"
    assert verifier_log.read_text().splitlines() == verifier_calls


def test_source_install_waits_for_shared_closer_coordination_lock(tmp_path, monkeypatch):
    import closer_native_lease as lease

    started = tmp_path / "install-started"
    monkeypatch.setenv("INSTALL_STARTED", str(started))
    journal, _mirror, _generation, call = make_fake_release(tmp_path, monkeypatch, invoke=False)
    lane = Path.home() / ".codex/handoff/.lane-closer.lock"
    holder = lease._parent_lock(lease._coord_path(lane))
    try:
        with pytest.raises(ReleaseHeld, match="coordination lock contended"):
            call()
        held = journal.read()
        assert held["stage"] == "verified"
        assert held["failure"] is None
        assert not started.exists(), "installer ran while the native coordination lock was held"
    finally:
        lease._unlock(holder)

    assert call()["stage"] == "installed"
    assert started.read_text() == "started"
    assert journal.read()["stage"] == "installed"


def test_source_holds_shared_closer_coordination_lock_through_install_and_readback(
    tmp_path, monkeypatch
):
    import closer_native_lease as lease

    install_started = tmp_path / "install-started"
    install_release = tmp_path / "install-release"
    readback_started = tmp_path / "readback-started"
    readback_release = tmp_path / "readback-release"
    monkeypatch.setenv("INSTALL_STARTED", str(install_started))
    monkeypatch.setenv("INSTALL_RELEASE", str(install_release))
    monkeypatch.setenv("READBACK_STARTED", str(readback_started))
    monkeypatch.setenv("READBACK_RELEASE", str(readback_release))
    _journal, _mirror, _generation, call = make_fake_release(tmp_path, monkeypatch, invoke=False)
    outcome = {}

    def install():
        try:
            outcome["result"] = call()
        except BaseException as exc:  # Assert it in the parent after cleanup.
            outcome["error"] = exc

    worker = threading.Thread(target=install, daemon=True)
    worker.start()
    contender = None
    acquired = threading.Event()
    try:
        assert wait_for_path(install_started), "installer did not reach its bounded test gate"

        lane = Path.home() / ".codex/handoff/.lane-closer.lock"

        def contend():
            handle = lease._parent_lock(lease._coord_path(lane))
            try:
                acquired.set()
            finally:
                lease._unlock(handle)

        contender = threading.Thread(target=contend, daemon=True)
        contender.start()
        assert not acquired.wait(0.2)
        install_release.touch()
        assert wait_for_path(readback_started), "readback did not reach its bounded test gate"
        assert not acquired.wait(0.2), "coordination lock was released before readback"
    finally:
        install_release.touch()
        readback_release.touch()
        worker.join(timeout=10)
        if contender is not None:
            contender.join(timeout=10)
        assert not worker.is_alive()
        assert contender is None or not contender.is_alive()

    assert "error" not in outcome
    assert outcome["result"]["stage"] == "installed"
    assert acquired.is_set()
