"""Automatic publishers compare the incumbent they observed before verification."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from test_install_verified_snapshot import installer


def payloads(tmp_path):
    from test_install_verified_snapshot import _snapshot

    old = _snapshot(tmp_path / "old")
    new = _snapshot(tmp_path / "new")
    (old / "module.py").write_text("VALUE = 'old'\n")
    (new / "module.py").write_text("VALUE = 'new'\n")
    return old, new, tmp_path / "mirror", tmp_path / "runtime" / "registry.json"


def publish(snapshot, mirror, registry, **kw):
    return installer.install(snapshot, mirror, installer.snapshot_digest(snapshot), registry, **kw)


@pytest.mark.parametrize("incumbent", ["absent", "legacy", "generation"])
def test_expected_incumbent_accepts_absent_legacy_and_generation(tmp_path, incumbent):
    old, new, mirror, registry = payloads(tmp_path)
    if incumbent == "legacy":
        mirror.mkdir()
    elif incumbent == "generation":
        publish(old, mirror, registry)
    token = installer.generation_token(mirror)
    assert token == installer.generation_token(mirror)
    publish(new, mirror, registry, expected_generation=token)
    assert (mirror / "module.py").read_text() == "VALUE = 'new'\n"
    assert installer.generation_token(mirror) != token


def test_stale_automatic_publisher_cannot_replace_new_generation_or_registry(tmp_path):
    old, new, mirror, registry = payloads(tmp_path)
    captured = installer.generation_token(mirror)
    publish(new, mirror, registry)
    before = (mirror.readlink(), registry.read_bytes(), installer.snapshot_digest(mirror))
    with pytest.raises(ValueError, match="generation changed during verification"):
        publish(old, mirror, registry, expected_generation=captured)
    assert (mirror.readlink(), registry.read_bytes(), installer.snapshot_digest(mirror)) == before
    assert not list(tmp_path.glob("mirror.next-*"))


def test_concurrent_automatic_publishers_compare_after_exclusive_lock(tmp_path):
    old, new, mirror, registry = payloads(tmp_path)
    captured = installer.generation_token(mirror)
    barrier = threading.Barrier(2)
    outcomes = []

    def writer(snapshot):
        barrier.wait(timeout=10)
        try:
            publish(snapshot, mirror, registry, expected_generation=captured)
            outcomes.append("published")
        except ValueError as exc:
            assert "generation changed" in str(exc)
            outcomes.append("stale")

    threads = [threading.Thread(target=writer, args=(s,)) for s in (old, new)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive()
    assert sorted(outcomes) == ["published", "stale"]
    assert installer.snapshot_digest(mirror) in {
        installer.snapshot_digest(old),
        installer.snapshot_digest(new),
    }


def test_manual_rollback_without_optional_cas_is_preserved(tmp_path):
    old, new, mirror, registry = payloads(tmp_path)
    publish(new, mirror, registry)
    publish(old, mirror, registry)
    assert (mirror / "module.py").read_text() == "VALUE = 'old'\n"


def test_cas_does_not_replace_snapshot_integrity_validation(tmp_path):
    old, new, mirror, registry = payloads(tmp_path)
    token = installer.generation_token(mirror)
    digest = installer.snapshot_digest(new)
    (new / "module.py").write_text("tampered\n")
    with pytest.raises(ValueError, match="retained snapshot digest"):
        installer.install(new, mirror, digest, registry, expected_generation=token)
    assert not mirror.exists() and not registry.exists()
    with pytest.raises(ValueError, match="captured generation-v1"):
        publish(old, mirror, registry, expected_generation="invalid")


def test_documented_automatic_wrapper_rejects_late_publication(tmp_path):
    import os
    import subprocess
    import time

    from test_mirror_sync_patch import _documented_patch

    old, new, mirror, registry = payloads(tmp_path)
    source = tmp_path / "source"
    scripts = source / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "install_verified_snapshot.py").write_bytes(Path(installer.__file__).read_bytes())
    begun, release = tmp_path / "begun", tmp_path / "release"
    pre = scripts / "verify_before_sync.sh"
    pre.write_text(
        "#!/bin/bash\nset -euo pipefail\n"
        'touch "$TEST_BEGUN"\n'
        'for n in {1..200}; do [[ -e "$TEST_RELEASE" ]] && break; sleep .05; done\n'
        '[[ -e "$TEST_RELEASE" ]] || exit 1\n'
        'cp -R "$TEST_OLD" "$2"\n'
        'printf "%s\\n" "$TEST_DIGEST" > "$4"\n'
    )
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text(
        "#!/bin/bash\nset -euo pipefail\nRUN_VERIFY=1\nALLOW_UNMERGED=0\n"
        'SRC="$TEST_SOURCE"\nMIRROR="$TEST_MIRROR"\n' + _documented_patch() + "\n"
    )
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "TMPDIR": str(temporary),
        "TEST_BEGUN": str(begun),
        "TEST_RELEASE": str(release),
        "TEST_OLD": str(old),
        "TEST_DIGEST": installer.snapshot_digest(old),
        "TEST_SOURCE": str(source),
        "TEST_MIRROR": str(mirror),
    }
    child = subprocess.Popen(
        ["bash", str(wrapper)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        deadline = time.monotonic() + 8
        while not begun.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert begun.exists()
        publish(new, mirror, registry)
        before = mirror.readlink(), registry.read_bytes()
        release.touch()
        stdout, stderr = child.communicate(timeout=15)
        assert child.returncode == 2, (stdout, stderr)
        assert "generation changed during verification" in stderr
        assert (mirror.readlink(), registry.read_bytes()) == before
    finally:
        release.touch()
        if child.poll() is None:
            child.communicate(timeout=15)
