"""Exact lease cleanup receipts and real bash EXIT propagation; isolated state only."""

from __future__ import annotations

import concurrent.futures
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import closer_native_lease as lease

ROOT = Path(__file__).resolve().parents[1]
LEASE = Path(lease.__file__).resolve()
_spec = importlib.util.spec_from_file_location(
    "cleanup_receipt_renderer", ROOT / "scripts/render_closer_native_hooks.py"
)
assert _spec and _spec.loader
renderer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(renderer)


@pytest.fixture
def root(tmp_path):
    value = tmp_path / "handoff with spaces"
    value.mkdir()
    return value


@pytest.mark.parametrize("full_meta", [False, True])
def test_receipt_attests_only_original_matched_removal(root, full_meta):
    token = lease.acquire(root, "closer", "cursor", 42)
    lane = root / ".lane-closer.lock"
    original = lease.read_meta_bytes(lane)
    inode = lane.stat().st_ino
    credential = original.decode() if full_meta else token
    ok, receipt = lease.release_with_receipt(root, "closer", credential)
    assert ok and receipt is not None
    assert receipt["schema"] == "closer-native-lease-cleanup/v1"
    assert receipt["lane_dir"] == str(lane.resolve())
    assert receipt["released_lease_inode"] == inode
    assert receipt["expected_token_sha256"] == hashlib.sha256(credential.encode()).hexdigest()
    assert receipt["meta_sha256"] == hashlib.sha256(original).hexdigest()
    assert receipt["released"] is True and receipt["exact_token_matched"] is True
    assert receipt["released_at"] > 0
    assert not lane.exists()
    assert lease.release_with_receipt(root, "closer", credential) == (True, None)
    assert token not in json.dumps(receipt)  # Do not publish the reusable credential.


def test_concurrent_cleanup_yields_one_actual_release_receipt(root):
    lease.acquire(root, "closer", "cursor", 42)
    credential = lease.read_meta_bytes(root / ".lane-closer.lock").decode()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: lease.release_with_receipt(root, "closer", credential), range(2))
        )
    assert all(ok for ok, _receipt in results)
    assert sum(receipt is not None for _ok, receipt in results) == 1


def test_successor_and_unknown_files_never_receive_cleanup_receipt(root):
    old = lease.acquire(root, "closer", "cursor", 42)
    assert lease.release(root, "closer", old)
    lease.acquire(root, "closer", "cursor", 43)
    lane = root / ".lane-closer.lock"
    before = lease.read_meta_bytes(lane)
    assert lease.release_with_receipt(root, "closer", old) == (False, None)
    assert lease.read_meta_bytes(lane) == before
    (lane / "uncooperative-writer").write_text("preserve")
    assert lease.release_with_receipt(root, "closer", before.decode()) == (False, None)
    assert lease.read_meta_bytes(lane) == before


def _postrun(tmp_path, root, credential, body_rc=65):
    original = textwrap.dedent(f"""\
        #!/bin/bash
        set -euo pipefail
        lane="${{1:-closer}}"
        LANE_MUTEX_DIR="${{HANDOFF_LANE_MUTEX_DIR}}"
        launch_keepalive_label_repair() {{ :; }}
        trap 'rm -rf "$LANE_MUTEX_DIR" 2>/dev/null || true; launch_keepalive_label_repair' EXIT
        exit {body_rc}
    """)
    script = tmp_path / "postrun with spaces.sh"
    script.write_bytes(renderer.patch_postrun(original.encode()))
    nonce = "a" * 48
    env = {
        **os.environ,
        "HANDOFF_HANDOFF_ROOT": str(root),
        "HANDOFF_LANE_MUTEX_DIR": str(root / ".lane-closer.lock"),
        "HANDOFF_EXPECTED_LANE_TOKEN": credential,
        "CLOSER_NATIVE_LEASE_PY": str(LEASE),
        "CLOSER_NATIVE_LEASE_INTERPRETER": sys.executable,
        "CLOSER_ADMISSION_NONCE": nonce,
    }
    done = subprocess.run(
        ["/bin/bash", str(script), "closer"], env=env, capture_output=True, text=True, check=False
    )
    return done, nonce


def test_real_postrun_C65_preserves_failure_and_records_exact_cleanup(root, tmp_path):
    lease.acquire(root, "closer", "cursor", 42)
    lane = root / ".lane-closer.lock"
    credential = lease.read_meta_bytes(lane).decode()
    done, nonce = _postrun(tmp_path, root, credential)
    assert done.returncode == 65
    prefix = "CLOSER_NATIVE_CLEANUP_V1 " + nonce + " "
    lines = [line for line in done.stdout.splitlines() if line.startswith(prefix)]
    assert len(lines) == 1
    receipt = json.loads(lines[0][len(prefix) :])
    assert receipt["released"] is True and not lane.exists()
    assert receipt["expected_token_sha256"] == hashlib.sha256(credential.encode()).hexdigest()
    assert credential not in done.stdout


def test_real_postrun_successor_refusal_overrides_body_and_emits_no_receipt(root, tmp_path):
    old = lease.acquire(root, "closer", "cursor", 42)
    assert lease.release(root, "closer", old)
    lease.acquire(root, "closer", "cursor", 43)
    lane = root / ".lane-closer.lock"
    before = lease.read_meta_bytes(lane)
    done, _nonce = _postrun(tmp_path, root, old)
    assert done.returncode == 2
    assert "CLOSER_NATIVE_CLEANUP_V1" not in done.stdout
    assert lease.read_meta_bytes(lane) == before


def test_real_postrun_missing_lease_cannot_backfill_exact_cleanup(root, tmp_path):
    done, _nonce = _postrun(tmp_path, root, "old missing credential")
    assert done.returncode == 4
    assert "CLOSER_NATIVE_CLEANUP_V1" not in done.stdout


def test_cli_legacy_absence_stays_idempotent_but_receipt_mode_refuses(root):
    args = [
        sys.executable,
        str(LEASE),
        "--handoff-root",
        str(root),
        "release",
        "--lane",
        "closer",
        "--expected-token",
        "absent",
    ]
    legacy = subprocess.run(args, capture_output=True, text=True, check=False)
    strict = subprocess.run([*args, "--receipt-json"], capture_output=True, text=True, check=False)
    assert legacy.returncode == 0 and legacy.stdout == ""
    assert strict.returncode == 4 and strict.stdout == ""


@pytest.mark.parametrize("dependencies_valid", [True, False])
@pytest.mark.parametrize("include_nonce", [True, False])
def test_bounded_postrun_distinguishes_named_refusal_from_dependency_failure(
    root, tmp_path, dependencies_valid, include_nonce
):
    lease.acquire(root, "closer", "cursor", 42)
    lane = root / ".lane-closer.lock"
    credential = lease.read_meta_bytes(lane).decode()
    original = textwrap.dedent("""\
        #!/bin/bash
        set -euo pipefail
        lane="${1:-closer}"
        LANE_MUTEX_DIR="${HANDOFF_LANE_MUTEX_DIR}"
        launch_keepalive_label_repair() { :; }
        trap 'rm -rf "$LANE_MUTEX_DIR" 2>/dev/null || true; launch_keepalive_label_repair' EXIT
        # BEGIN CLOSER STATUS DISPATCHER STOP GATE v1
        python3 "$_closer_dispatcher" validate-stop --state "$CLOSER_ROUND_STATE" --assignment "$HOME/.codex/automations/reviewed-repo-automation-controller/closer-assignment.json"
        "$HANDOFF_BIN" end-round "$outcome"
        if "$RELAY_BIN" "$relay_lane"; then :; fi
    """)
    script = tmp_path / "bounded postrun.sh"
    script.write_bytes(renderer.patch_postrun(original.encode(), bounded_round=True))
    nonce = "b" * 48
    env = {
        **os.environ,
        "HANDOFF_HANDOFF_ROOT": str(root),
        "HANDOFF_LANE_MUTEX_DIR": str(lane),
        "HANDOFF_EXPECTED_LANE_TOKEN": credential,
        "CLOSER_NATIVE_LEASE_PY": str(LEASE),
        "CLOSER_NATIVE_LEASE_INTERPRETER": sys.executable,
        "CLOSER_ADMISSION_NONCE": nonce if include_nonce else "",
        "CLOSER_POSTRUN_DEPENDENCIES_VALID": "1" if dependencies_valid else "0",
        "CLOSER_POSTRUN_REQUIRE_NAMED_ROUND": "1",
        "CLOSER_ROUND_STATE": str(tmp_path / "round state.json"),
        "CLOSER_POSTRUN_ASSIGNMENT": str(tmp_path / "admitted assignment.json"),
    }
    env.pop("CLOSER_POSTRUN_ROUND_ID", None)
    done = subprocess.run(
        ["/bin/bash", str(script), "closer"], env=env, capture_output=True, text=True, check=False
    )
    assert done.returncode == 65 and not lane.exists()
    lines = done.stdout.splitlines()
    assert sum(line.startswith("CLOSER_NATIVE_CLEANUP_V1 " + nonce + " ") for line in lines) == int(
        include_nonce
    )
    assert (
        "CLOSER_NATIVE_POSTRUN_REFUSAL_V1 " + nonce + " terminal_named_receipt_unavailable" in lines
    ) is (dependencies_valid and include_nonce)
    assert "unbound variable" not in done.stderr
