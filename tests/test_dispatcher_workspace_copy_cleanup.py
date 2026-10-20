"""Failed bounded workspace copies clean only proven terminal, owned partials."""

import signal
import subprocess
from pathlib import Path

import pytest

import dispatcher


def copier(monkeypatch, tmp_path, *, outcome="nonzero", denied=False, successor=False):
    source = tmp_path / "source"
    source.mkdir()
    offloads = tmp_path / "offloads"
    monkeypatch.setattr(dispatcher, "OFFLOAD_DIR", offloads)
    real_popen = subprocess.Popen
    real_killpg = dispatcher.os.killpg
    calls = []
    partials = []

    class Copy:
        pid = 987654321
        returncode = None
        waits = 0

        def communicate(self, timeout):
            self.waits += 1
            calls.append(("communicate", timeout))
            if outcome == "nonzero":
                self.returncode = 1
                return "", "injected copy failure"
            if self.waits == 1 or outcome == "unreaped":
                raise subprocess.TimeoutExpired("owned copier", timeout)
            self.returncode = -signal.SIGKILL
            return "", ""

        def poll(self):
            return self.returncode

    proc = Copy()

    def spawn(command, **kwargs):
        if "shutil.copytree(src,dst" not in command[2]:
            return real_popen(command, **kwargs)
        partial = Path(command[4])
        partial.mkdir(exist_ok=True)
        (partial / "payload").write_text("partial copy")
        partials.append(partial)
        if successor:
            partial.rename(partial.with_suffix(".original"))
            partial.mkdir()
            (partial / "successor").write_text("preserve")
        return proc

    def killpg(pid, sig):
        if pid != proc.pid:
            return real_killpg(pid, sig)
        calls.append(("signal", sig))
        if sig == 0 and proc.returncode is not None:
            raise ProcessLookupError("copier group absent")
        if denied:
            raise PermissionError("owned copier termination denied")

    monkeypatch.setattr(dispatcher.subprocess, "Popen", spawn)
    monkeypatch.setattr(dispatcher.os, "killpg", killpg)
    return source, partials, calls


@pytest.mark.parametrize("outcome", ["nonzero", "timeout"])
def test_terminal_failed_copy_removes_owned_partial(monkeypatch, tmp_path, outcome):
    source, partials, calls = copier(monkeypatch, tmp_path, outcome=outcome)
    error = RuntimeError if outcome == "nonzero" else TimeoutError
    with pytest.raises(error, match="bounded offload workspace copy"):
        dispatcher._isolate_offload_cwd(source, timeout=0.25)
    assert partials and not partials[0].exists()
    assert not list((tmp_path / "offloads").glob("*"))
    assert calls[0] == ("communicate", 0.25)


def test_second_copy_timeout_preserves_partial_and_original_error(monkeypatch, tmp_path):
    source, partials, calls = copier(monkeypatch, tmp_path, outcome="unreaped")
    with pytest.raises(TimeoutError, match="terminality unverified.*partial retained"):
        dispatcher._isolate_offload_cwd(source, timeout=0.25)
    assert (partials[0] / "payload").read_text() == "partial copy"
    assert [row for row in calls if row[0] == "signal"] == [("signal", signal.SIGKILL)]
    assert [row[1] for row in calls if row[0] == "communicate"] == [0.25, 2]


def test_denied_copy_kill_preserves_partial_without_alternate_signal(monkeypatch, tmp_path):
    source, partials, calls = copier(monkeypatch, tmp_path, outcome="timeout", denied=True)
    with pytest.raises(PermissionError, match="termination denied"):
        dispatcher._isolate_offload_cwd(source, timeout=0.25)
    assert (partials[0] / "payload").exists()
    assert [row for row in calls if row[0] == "signal"] == [("signal", signal.SIGKILL)]


def test_failed_copy_cannot_remove_successor_partial(monkeypatch, tmp_path):
    source, partials, _ = copier(monkeypatch, tmp_path, successor=True)
    with pytest.raises(RuntimeError, match="partial retained"):
        dispatcher._isolate_offload_cwd(source, timeout=0.25)
    assert (partials[0] / "successor").read_text() == "preserve"


def test_reaped_copier_with_live_group_retains_partial(monkeypatch, tmp_path):
    source, partials, _ = copier(monkeypatch, tmp_path)
    monkeypatch.setattr(dispatcher.os, "killpg", lambda *_: None)
    with pytest.raises(RuntimeError, match="partial retained"):
        dispatcher._isolate_offload_cwd(source, timeout=0.25)
    assert (partials[0] / "payload").exists()


@pytest.mark.parametrize("denied", [False, True])
def test_cleanup_timeout_is_bounded_and_never_retries_denied_kill(monkeypatch, tmp_path, denied):
    partial = tmp_path / "owned.partial"
    partial.mkdir()
    (partial / "payload").write_text("preserve uncertainty")
    identity = partial.stat()
    calls = []

    class Cleanup:
        pid = 987654322
        returncode = None

        def communicate(self, timeout):
            calls.append(("wait", timeout))
            raise subprocess.TimeoutExpired("owned cleanup", timeout)

    monkeypatch.setattr(dispatcher.subprocess, "Popen", lambda *_args, **_kwargs: Cleanup())

    def killpg(pid, sig):
        calls.append(("signal", pid, sig))
        if denied:
            raise PermissionError("owned cleanup denied")

    monkeypatch.setattr(dispatcher.os, "killpg", killpg)
    if denied:
        with pytest.raises(PermissionError, match="cleanup denied"):
            dispatcher._remove_owned_offload_partial(partial, (identity.st_dev, identity.st_ino))
    else:
        assert not dispatcher._remove_owned_offload_partial(
            partial, (identity.st_dev, identity.st_ino)
        )
    assert (partial / "payload").exists()
    assert [row for row in calls if row[0] == "signal"] == [("signal", 987654322, signal.SIGKILL)]
    assert [row for row in calls if row[0] == "wait"] == [("wait", 2)] * (1 if denied else 2)


def test_cleanup_child_rechecks_identity_after_spawn(monkeypatch, tmp_path):
    partial = tmp_path / "owned.partial"
    partial.mkdir()
    identity = partial.stat()
    real_popen = subprocess.Popen

    def spawn(command, **kwargs):
        partial.rename(tmp_path / "original")
        partial.mkdir()
        (partial / "successor").write_text("preserve")
        return real_popen(command, **kwargs)

    monkeypatch.setattr(dispatcher.subprocess, "Popen", spawn)
    assert not dispatcher._remove_owned_offload_partial(partial, (identity.st_dev, identity.st_ino))
    assert (partial / "successor").read_text() == "preserve"


def test_denied_terminal_probe_preserves_partial_and_denial(monkeypatch, tmp_path):
    source, partials, _ = copier(monkeypatch, tmp_path)
    signals = []

    def denied(pid, sig):
        signals.append(sig)
        raise PermissionError("copier group observation denied")

    monkeypatch.setattr(dispatcher.os, "killpg", denied)
    with pytest.raises(PermissionError, match="observation denied"):
        dispatcher._isolate_offload_cwd(source, timeout=0.25)
    assert signals == [0]
    assert (partials[0] / "payload").exists()


def test_real_failed_copier_removes_partial_tree(monkeypatch, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload").write_text("copied before failure")
    (source / "missing-link").symlink_to(source / "absent")
    offloads = tmp_path / "offloads"
    monkeypatch.setattr(dispatcher, "OFFLOAD_DIR", offloads)
    with pytest.raises(RuntimeError, match="bounded offload workspace copy failed"):
        dispatcher._isolate_offload_cwd(source, timeout=5)
    assert not list(offloads.iterdir())
