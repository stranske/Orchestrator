"""Guard shell units isolate inherited storage; production lock refusal remains real."""

import fcntl
from pathlib import Path

import pytest
from test_agent_gh_guard import _agent, _logged
from test_agent_gh_guard import world as world

import dispatcher
import storage_lifecycle as storage


@pytest.fixture
def owned_context(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "owned-job"
    root.mkdir(mode=0o700)
    control = tmp_path.resolve() / "owned-control"
    manager = storage.Reservations(control, tmp_path, floor=0, free=lambda _: 100000)
    manager.admit("guardfixture", "fixture", 0, 1, owner_token="synthetic-owner")
    storage.write_json(root / "owner.json", {"job": "guardfixture", "token": "synthetic-owner"})
    info = root.stat()
    manager.bind("guardfixture", root=str(root), root_identity=[info.st_dev, info.st_ino])
    env = {
        "ORCH_STORAGE_RUN_ROOT": str(root),
        "ORCH_STORAGE_RUN_ID": "guardfixture",
        "ORCH_STORAGE_OWNER_TOKEN": "synthetic-owner",
        "ORCH_STORAGE_STATE_DIR": str(control),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


def test_production_prelude_refuses_exclusive_storage_lock_before_command(
    owned_context, monkeypatch
):
    def forbidden(*args, **kwargs):
        pytest.fail("storage refusal must precede any shell command")

    monkeypatch.setattr(dispatcher.subprocess, "run", forbidden)
    lock = Path(owned_context["ORCH_STORAGE_STATE_DIR"]) / "lock"
    with lock.open("rb") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(storage.StorageBlocked, match="BlockingIOError"):
            dispatcher._agent_runtime_prelude("codex")
    assert storage.owned_agent_tmpdir("codex", owned_context).is_relative_to(
        Path(owned_context["ORCH_STORAGE_RUN_ROOT"])
    )


@pytest.mark.parametrize("exclusive_lock", [False, True])
def test_guard_world_isolates_inherited_storage_before_its_prelude(
    owned_context, request, exclusive_lock
):
    lock = Path(owned_context["ORCH_STORAGE_STATE_DIR"]) / "lock"
    with lock.open("rb") as handle:
        if exclusive_lock:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        isolated = request.getfixturevalue("world")
        prelude = dispatcher._agent_runtime_prelude("codex")
        assert owned_context["ORCH_STORAGE_RUN_ROOT"] not in prelude
        ran = _agent(isolated, "exec gh -R o/r auth logout", prelude=prelude)
        assert ran.returncode == dispatcher.GH_GUARD_EXIT, ran.stderr
        assert _logged(isolated) == ""
        if exclusive_lock:
            # Fixture isolation does not unlock or relax the real storage validator.
            with pytest.raises(storage.StorageBlocked, match="BlockingIOError"):
                storage.owned_agent_tmpdir("codex", owned_context)
            assert not (Path(owned_context["ORCH_STORAGE_RUN_ROOT"]) / "tmp").exists()
