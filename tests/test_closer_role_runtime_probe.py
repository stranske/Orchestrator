"""Offline eligibility-cache checks; actual launch construction keeps a fresh hash."""

import hashlib
import logging
import os
from types import SimpleNamespace

import pytest
from test_closer_role_qualification import NOW, record

import adapters
import closer_role_qualification as roles
import execution_profiles


@pytest.fixture
def binary_probe(tmp_path, monkeypatch):
    binary = tmp_path / "offline-claude"
    contents = b"offline data-only CLI fixture"
    binary.write_bytes(contents)
    binary.chmod(0o700)
    digest = hashlib.sha256(contents).hexdigest()
    monkeypatch.setattr(
        adapters.shutil, "which", lambda name: str(binary) if name == "claude" else None
    )
    monkeypatch.setattr(adapters, "DATA_ONLY_CLAUDE_SHA256", digest)
    monkeypatch.setattr(adapters, "_DATA_ONLY_CLAUDE_PROBE_CACHE", {})
    clock = {"now": 100.0}
    monkeypatch.setattr(adapters, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    full_probe = adapters._data_only_claude_binary
    reads = []

    def observed_probe():
        reads.append("full-binary-hash")
        return full_probe()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("runtime probe test attempted a CLI/provider call")

    monkeypatch.setattr(adapters, "_data_only_claude_binary", observed_probe)
    monkeypatch.setattr(adapters.subprocess, "run", forbidden)
    monkeypatch.setattr(adapters.subprocess, "Popen", forbidden)
    return binary, contents, clock, reads


def test_successful_eligibility_is_cached_by_all_actual_file_identity_fields(binary_probe):
    binary, _contents, _clock, reads = binary_probe
    assert adapters.data_only_problem("claude", "assess") is None
    info = binary.stat()
    assert adapters._data_only_claude_probe_key() == (
        str(binary.resolve()),
        (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
            info.st_mode,
            info.st_uid,
            info.st_gid,
        ),
        adapters.DATA_ONLY_CLAUDE_SHA256,
    )
    for _ in range(10):
        assert adapters.data_only_problem("claude", "assess") is None
    assert reads == ["full-binary-hash"]


def test_short_monotonic_expiry_forces_full_recheck(binary_probe):
    _binary, _contents, clock, reads = binary_probe
    assert adapters.data_only_problem("claude", "assess") is None
    clock["now"] += adapters._DATA_ONLY_CLAUDE_PROBE_TTL_SECONDS - 0.001
    assert adapters.data_only_problem("claude", "assess") is None
    assert len(reads) == 1
    clock["now"] += 0.002
    assert adapters.data_only_problem("claude", "assess") is None
    assert len(reads) == 2


def test_binary_drift_invalidates_success_and_failure_is_not_cached(binary_probe):
    binary, contents, _clock, reads = binary_probe
    assert adapters.data_only_problem("claude", "assess") is None
    before = binary.stat()
    binary.write_bytes(b"X" * len(contents))
    os.utime(binary, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000000))
    for _ in range(2):
        assert "not qualified" in adapters.data_only_problem("claude", "assess")
    assert len(reads) == 3


def test_mode_change_invalidates_even_an_unchanged_binary(binary_probe):
    binary, _contents, _clock, reads = binary_probe
    assert adapters.data_only_problem("claude", "assess") is None
    binary.chmod(0o500)
    assert adapters.data_only_problem("claude", "assess") is None
    assert len(reads) == 2


def test_resolved_symlink_path_change_invalidates_success(binary_probe, tmp_path, monkeypatch):
    binary, contents, _clock, reads = binary_probe
    link = tmp_path / "claude-link"
    link.symlink_to(binary)
    monkeypatch.setattr(adapters.shutil, "which", lambda _name: str(link))
    assert adapters.data_only_problem("claude", "assess") is None
    replacement = tmp_path / "replacement-claude"
    replacement.write_bytes(contents)
    replacement.chmod(0o700)
    link.unlink()
    link.symlink_to(replacement)
    assert adapters.data_only_problem("claude", "assess") is None
    assert len(reads) == 2


def test_expected_hash_change_cannot_reuse_prior_success(binary_probe, monkeypatch):
    _binary, _contents, _clock, reads = binary_probe
    assert adapters.data_only_problem("claude", "assess") is None
    monkeypatch.setattr(adapters, "DATA_ONLY_CLAUDE_SHA256", "f" * 64)
    assert "not qualified" in adapters.data_only_problem("claude", "assess")
    assert len(reads) == 2


def test_absence_never_returns_a_cached_success(binary_probe, monkeypatch):
    _binary, _contents, _clock, reads = binary_probe
    assert adapters.data_only_problem("claude", "assess") is None
    monkeypatch.setattr(adapters.shutil, "which", lambda _name: None)
    assert "absent" in adapters.data_only_problem("claude", "assess")
    assert len(reads) == 1


def test_before_after_probe_identity_drift_is_not_cached(binary_probe, monkeypatch):
    binary, _contents, _clock, reads = binary_probe
    full_probe = adapters._data_only_claude_binary

    def mutate_after_full_hash():
        verified = full_probe()
        info = binary.stat()
        os.utime(binary, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000000))
        return verified

    with monkeypatch.context() as scope:
        scope.setattr(adapters, "_data_only_claude_binary", mutate_after_full_hash)
        assert "changed during eligibility" in adapters.data_only_problem("claude", "assess")
    assert adapters._DATA_ONLY_CLAUDE_PROBE_CACHE == {}
    assert adapters.data_only_problem("claude", "assess") is None
    assert len(reads) == 2


def test_cache_hit_rechecks_file_identity_after_lookup(binary_probe, monkeypatch):
    _binary, _contents, _clock, reads = binary_probe
    assert adapters.data_only_problem("claude", "assess") is None
    key = adapters._data_only_claude_probe_key()
    changed = (key[0], (*key[1][:-1], key[1][-1] + 1), key[2])
    keys = iter([key, changed])
    monkeypatch.setattr(adapters, "_data_only_claude_probe_key", lambda: next(keys))
    assert "changed during eligibility" in adapters.data_only_problem("claude", "assess")
    assert len(reads) == 1


def test_actual_launch_construction_always_rehashes_despite_eligibility_cache(
    binary_probe, monkeypatch
):
    binary, contents, _clock, reads = binary_probe
    assert adapters.data_only_problem("claude", "assess") is None
    for _ in range(2):
        command = adapters.build_command(
            "claude",
            "",
            mode="assess",
            transport="offload",
            data_only=True,
            prompt_via_stdin=True,
            profile="claude-sonnet-5-5-low",
        )
        assert command[0] == str(binary)
    assert len(reads) == 3
    # Simulate a metadata blind spot: even a forced cached key cannot bypass
    # the unchanged full content check in build_command's actual-launch path.
    cached_key = adapters._data_only_claude_probe_key()
    monkeypatch.setattr(adapters, "_data_only_claude_probe_key", lambda: cached_key)
    binary.write_bytes(b"X" * len(contents))
    with pytest.raises(ValueError, match="not qualified"):
        adapters.build_command(
            "claude",
            "",
            mode="assess",
            transport="offload",
            data_only=True,
            prompt_via_stdin=True,
            profile="claude-sonnet-5-5-low",
        )
    assert len(reads) == 4


def runtime_record():
    profile = execution_profiles.get_profile("claude-sonnet-5-5-low")
    proof = record(profile, roles.BOUNDED_CLASSIFICATION)
    proof["confinement"]["adapter_build_sha256"] = adapters.DATA_ONLY_CLAUDE_SHA256
    return profile, proof


@pytest.mark.parametrize("change", ["record_expired", "registry_drift", "retired"])
def test_role_record_registry_and_retirement_are_not_cached(binary_probe, monkeypatch, change):
    _binary, _contents, _clock, reads = binary_probe
    profile, proof = runtime_record()
    records = {proof["qualification_id"]: proof}
    assert roles.is_runtime_qualified(
        profile, roles.BOUNDED_CLASSIFICATION, now=NOW, records=records
    )
    if change == "record_expired":
        proof["expires_at"] = NOW - 1
    elif change == "registry_drift":
        monkeypatch.setitem(
            execution_profiles.PROFILE_REGISTRY,
            profile["profile_id"],
            dict(profile, prompt_version="changed"),
        )
    else:
        monkeypatch.setitem(
            execution_profiles.PROFILE_RETIREMENTS, profile["profile_id"], "other-profile"
        )
    assert not roles.is_runtime_qualified(
        profile, roles.BOUNDED_CLASSIFICATION, now=NOW, records=records
    )
    assert len(reads) == 1


@pytest.mark.parametrize(
    "error",
    [
        OSError("synthetic probe error"),
        ValueError("synthetic probe error"),
        RuntimeError("synthetic probe error"),
        None,
    ],
)
def test_runtime_probe_errors_remain_false_and_are_debug_logged(
    binary_probe, monkeypatch, caplog, error
):
    profile, proof = runtime_record()

    def fail(*_args):
        if error is not None:
            raise error
        return "synthetic probe error"

    monkeypatch.setattr(adapters, "data_only_problem", fail)
    with caplog.at_level(logging.DEBUG, logger=roles.__name__):
        assert not roles.is_runtime_qualified(
            profile,
            roles.BOUNDED_CLASSIFICATION,
            now=NOW,
            records={proof["qualification_id"]: proof},
        )
    assert "synthetic probe error" in caplog.text
    assert profile["profile_id"] in caplog.text
