"""Transport and native queue tests must not use their verifier's storage lease."""

import os
import subprocess
import sys

import pytest

import paths
import storage_lifecycle as storage


@pytest.mark.parametrize(
    "node",
    [
        "tests/test_gemini_large_prompt_transport.py::test_nonempty_partial_stdout_does_not_hide_agy_in_progress_log",
        "tests/test_closer_verifier_attribution.py::test_public_cli_real_queue_typed_attribution_boundary[foreign]",
    ],
    ids=["gemini-transport", "native-attribution"],
)
def test_test_fixtures_do_not_inherit_verifier_storage(tmp_path, node):
    root = tmp_path.resolve() / "outer-job"
    root.mkdir(mode=0o700)
    manager = storage.Reservations(
        tmp_path / "outer-control", tmp_path, floor=0, free=lambda _: 10**9
    )
    manager.admit("outerjob", "fixture", 0, 1, owner_token="outer-token")
    storage.write_json(root / "owner.json", {"job": "outerjob", "token": "outer-token"})
    info = root.stat()
    manager.bind("outerjob", root=str(root), root_identity=[info.st_dev, info.st_ino])
    env = dict(
        os.environ,
        ORCH_STORAGE_RUN_ROOT=str(root),
        ORCH_STORAGE_RUN_ID="outerjob",
        ORCH_STORAGE_OWNER_TOKEN="outer-token",
        ORCH_STORAGE_STATE_DIR=str(manager.directory),
    )
    before = (manager.directory / "reservations.json").read_bytes()
    # Hold the real exclusive lock throughout the child: no scheduling luck or retries.
    # The child must own separate test storage even with a valid inherited release context.
    with manager.locked():
        child = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-n", "4", node],
            cwd=paths.REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert child.returncode == 0, child.stdout + child.stderr
        assert (manager.directory / "reservations.json").read_bytes() == before
    assert not list((manager.directory / "receipts").glob("*.json"))
    assert not (root / "tmp").exists()
