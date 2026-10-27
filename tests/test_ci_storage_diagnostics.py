"""Storage evidence stays bounded, metadata-only and non-authoritative."""

import errno
import json
import os
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

HELPER = Path(__file__).with_name("ci_storage_diagnostics.py")


def probe():
    return runpy.run_path(str(HELPER))["snapshot"]


def test_owned_tree_counts_metadata_without_following_external_symlink(tmp_path, monkeypatch):
    root = tmp_path / "owned"
    root.mkdir()
    child = root / "test_owned0"
    child.mkdir()
    (child / "small").write_bytes(b"x" * 17)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"private" * 1000)
    (child / "external").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr("shutil.which", lambda _name: None)
    value = probe()(root)
    assert value["tree"]["status"] == "complete"
    assert value["tree"]["logical_file_bytes_seen"] == 17
    assert value["tree"]["entries_seen"] == 3
    assert value["tree"]["largest_owned_children"][0][0] == "test_owned0"
    assert "secret" not in json.dumps(value)


def test_scan_limit_reports_partial_instead_of_total(tmp_path, monkeypatch):
    for number in range(3):
        (tmp_path / str(number)).write_text("x")
    fn = probe()
    fn.__globals__["MAX_ENTRIES"] = 1
    monkeypatch.setattr("shutil.which", lambda _name: None)
    value = fn(tmp_path)
    assert value["tree"]["status"] == "partial"
    assert value["tree"]["bounded"] is True
    assert value["tree"]["entries_seen"] == 1


def test_quota_observation_exposes_only_numeric_rows(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/quota")
    fn = probe()
    fn.__globals__["sys_platform_linux"] = lambda: True
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(
            returncode=0, stdout="private-account /private/path 10 20 30\n", stderr="secret"
        )

    monkeypatch.setattr("subprocess.run", run)
    value = fn(tmp_path)
    assert value["quota"]["numeric_rows"] == [[10, 20, 30]]
    assert "private" not in json.dumps(value) and "secret" not in json.dumps(value)
    assert calls[0][0][1:5] == ["-l", "-w", "-p", "-u"]
    assert calls[0][1]["timeout"] == 2


@pytest.mark.parametrize("failure", [OSError(errno.EDQUOT, "private-path"), TimeoutError()])
def test_storage_failure_cannot_mask_original_pytest_outcome(tmp_path, monkeypatch, failure):
    def unavailable(_path):
        raise failure

    monkeypatch.setattr(os, "fstatvfs", unavailable)
    value = probe()(tmp_path)
    assert value["status"] == "unavailable"
    assert "private-path" not in json.dumps(value)


def test_nonphysical_root_is_not_scanned(tmp_path):
    alias = tmp_path / "alias"
    actual = tmp_path / "actual"
    actual.mkdir()
    alias.symlink_to(actual, target_is_directory=True)
    assert probe()(alias)["reason"] == "nonphysical_base"


def test_inaccessible_owned_entry_is_reported(tmp_path, monkeypatch):
    fn = probe()
    monkeypatch.setattr("shutil.which", lambda _name: None)
    original = os.scandir

    def scandir(path):
        if isinstance(path, int):
            raise PermissionError("private-path")
        return original(path)

    monkeypatch.setattr(os, "scandir", scandir)
    value = fn(tmp_path)
    assert value["tree"]["inaccessible"] == 1
    assert value["tree"]["status"] == "partial"
    assert "private-path" not in json.dumps(value)


def test_replaced_directory_symlink_is_never_traversed(tmp_path, monkeypatch):
    root = tmp_path / "owned"
    root.mkdir()
    child = root / "child"
    child.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_bytes(b"s" * 31337)
    original_open = os.open
    replaced = []

    def replacement(path, flags, *args, **kwargs):
        if path == "child" and "dir_fd" in kwargs:
            child.rmdir()
            child.symlink_to(outside, target_is_directory=True)
            replaced.append(True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replacement)
    monkeypatch.setattr("shutil.which", lambda _name: None)
    value = probe()(root)
    assert replaced == [True]
    assert value["tree"]["logical_file_bytes_seen"] == 0
    assert value["tree"]["entries_seen"] == 1
    assert value["tree"]["inaccessible"] == 1
    assert value["tree"]["status"] == "partial"


def test_replaced_directory_inode_is_not_accepted(tmp_path, monkeypatch):
    root = tmp_path / "owned"
    root.mkdir()
    child = root / "child"
    child.mkdir()
    parked = root / "parked"
    original_open = os.open
    replaced = []

    def replacement(path, flags, *args, **kwargs):
        if path == "child" and "dir_fd" in kwargs:
            child.rename(parked)
            child.mkdir()
            (child / "new").write_bytes(b"new" * 100)
            replaced.append(True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replacement)
    monkeypatch.setattr("shutil.which", lambda _name: None)
    value = probe()(root)
    assert replaced == [True]
    assert value["tree"]["logical_file_bytes_seen"] == 0
    assert value["tree"]["inaccessible"] == 1
    assert value["tree"]["status"] == "partial"


def test_entry_bound_closes_all_owned_directory_descriptors(tmp_path, monkeypatch):
    (tmp_path / "child").mkdir()
    (tmp_path / "child/leaf").write_text("x")
    fn = probe()
    fn.__globals__["MAX_ENTRIES"] = 1
    opened = set()
    original_open, original_dup, original_close = os.open, os.dup, os.close

    def track_open(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.add(fd)
        return fd

    def track_dup(*args):
        fd = original_dup(*args)
        opened.add(fd)
        return fd

    def track_close(fd):
        original_close(fd)
        opened.discard(fd)

    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(os, "dup", track_dup)
    monkeypatch.setattr(os, "close", track_close)
    monkeypatch.setattr("shutil.which", lambda _name: None)
    result = fn(tmp_path)
    assert result["tree"]["bounded"] is True
    assert opened == set()
