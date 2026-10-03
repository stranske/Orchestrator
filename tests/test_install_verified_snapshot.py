from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

import pytest

import paths
from scripts import install_verified_snapshot as installer

INSTALLER = paths.REPO_ROOT / "scripts" / "install_verified_snapshot.py"


def _snapshot(tmp_path: Path) -> Path:
    snapshot = tmp_path / "verified snapshot"
    (snapshot / "scripts" / "nested").mkdir(parents=True)
    (snapshot / "tests" / "fixtures").mkdir(parents=True)
    (snapshot / "experiments").mkdir()
    (snapshot / "data").mkdir()
    (snapshot / "config").mkdir()
    (snapshot / ".github" / "workflows").mkdir(parents=True)
    (snapshot / "docs" / "briefs").mkdir(parents=True)
    (snapshot / "orchestrate.sh").write_text("#!/bin/sh\necho exact\n")
    (snapshot / "orchestrate.sh").chmod(0o751)
    (snapshot / "module.py").write_text("VALUE = 'verified'\n")
    (snapshot / "scripts" / INSTALLER.name).write_bytes(INSTALLER.read_bytes())
    (snapshot / "scripts" / INSTALLER.name).chmod(0o755)
    (snapshot / "scripts" / "nested" / "tool.sh").write_text("#!/bin/sh\n")
    (snapshot / "scripts" / "nested" / "tool.sh").chmod(0o750)
    (snapshot / "tests" / "test_one.py").write_text("def test_one(): assert True\n")
    (snapshot / "tests" / "fixtures" / "input.txt").write_text("fixture\n")
    (snapshot / "experiments" / "hypotheses.json").write_text("{}\n")
    (snapshot / "repo_review_registry.json").write_text('{"repos": []}\n')
    (snapshot / ".github" / "workflows" / "ci.yml").write_text("name: ci\n")
    (snapshot / ".gitignore").write_text(".coverage\n")
    (snapshot / "ruff.toml").write_text("line-length = 100\n")
    (snapshot / "docs" / "guide.md").write_text("new guide\n")
    (snapshot / "docs" / "briefs" / "one.md").write_text("one\n")
    (snapshot / ".docs-shipped.txt").write_text("docs/briefs/one.md\ndocs/guide.md\n")
    (snapshot / "__pycache__").mkdir()
    (snapshot / "__pycache__" / "module.pyc").write_bytes(b"generated")
    return snapshot


def _run(*args: str | Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", str(INSTALLER), *(str(arg) for arg in args)],
        capture_output=True,
        text=True,
    )


def _digest(snapshot: Path) -> str:
    result = _run(snapshot, "--digest")
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


def test_install_uses_only_snapshot_bytes_and_preserves_local_markers(tmp_path):
    snapshot = _snapshot(tmp_path)
    mirror = tmp_path / "live mirror"
    (mirror / "tests").mkdir(parents=True)
    (mirror / "tests" / "stale.py").write_text("stale\n")
    (mirror / "scripts").mkdir()
    (mirror / "scripts" / "stale.sh").write_text("stale\n")
    (mirror / "experiments").mkdir()
    (mirror / "experiments" / ".last-ship-gate").write_text("keep\n")
    (mirror / "experiments" / "hypotheses.json").write_text("old\n")
    (mirror / "stale.py").write_text("stale\n")
    (mirror / ".github").mkdir()
    (mirror / ".github" / "stale.yml").write_text("stale\n")
    (mirror / "docs" / "reports").mkdir(parents=True)
    (mirror / "docs" / "old.md").write_text("old\n")
    (mirror / "docs" / "reports" / "runtime.md").write_text("keep\n")
    (mirror / ".docs-shipped.txt").write_text("docs/old.md\n")
    registry = tmp_path / "runtime" / "repo_review_registry.json"

    result = _run(
        snapshot,
        mirror,
        "--expected-digest",
        _digest(snapshot),
        "--runtime-registry",
        registry,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (mirror / "module.py").read_text() == "VALUE = 'verified'\n"
    assert not (mirror / "stale.py").exists()
    assert not (mirror / "tests" / "stale.py").exists()
    assert not (mirror / "scripts" / "stale.sh").exists()
    assert not (mirror / ".github" / "stale.yml").exists()
    assert (mirror / ".github" / "workflows" / "ci.yml").is_file()
    assert not (mirror / "docs" / "old.md").exists()
    assert (mirror / "docs" / "reports" / "runtime.md").read_text() == "keep\n"
    assert (mirror / "docs" / "guide.md").read_text() == "new guide\n"
    assert (mirror / ".gitignore").read_text() == ".coverage\n"
    assert (mirror / "ruff.toml").read_text() == "line-length = 100\n"
    assert (mirror / "tests" / "fixtures" / "input.txt").read_text() == "fixture\n"
    assert (mirror / "experiments" / ".last-ship-gate").read_text() == "keep\n"
    assert (mirror / "experiments" / "hypotheses.json").read_text() == "{}\n"
    assert registry.read_text() == '{"repos": []}\n'
    assert not (mirror / "__pycache__").exists()
    assert os.stat(mirror / "orchestrate.sh").st_mode & 0o777 == 0o751
    assert os.stat(mirror / "scripts" / "nested" / "tool.sh").st_mode & 0o777 == 0o750


def test_digest_covers_content_and_permissions_but_not_generated_caches(tmp_path):
    snapshot = _snapshot(tmp_path)
    first = _run(snapshot, "--digest")
    assert first.returncode == 0, first.stdout + first.stderr
    (snapshot / "__pycache__" / "module.pyc").write_bytes(b"different cache")
    assert _run(snapshot, "--digest").stdout == first.stdout
    (snapshot / "module.py").write_text("VALUE = 'changed'\n")
    assert _run(snapshot, "--digest").stdout != first.stdout
    (snapshot / "module.py").write_text("VALUE = 'verified'\n")
    (snapshot / "orchestrate.sh").chmod(0o755)
    assert _run(snapshot, "--digest").stdout != first.stdout


def test_install_rejects_the_snapshot_as_its_own_destination(tmp_path):
    snapshot = _snapshot(tmp_path)
    result = _run(snapshot, snapshot, "--expected-digest", _digest(snapshot))
    assert result.returncode == 2
    assert "snapshot and mirror must be separate trees" in result.stderr


def test_install_validates_every_external_output_before_mutating_the_mirror(tmp_path):
    snapshot = _snapshot(tmp_path)
    mirror = tmp_path / "live mirror"
    mirror.mkdir()
    original = mirror / "old.py"
    original.write_text("still live\n")
    (snapshot / "repo_review_registry.json").write_text("not json\n")

    result = _run(
        snapshot,
        mirror,
        "--expected-digest",
        _digest(snapshot),
        "--runtime-registry",
        tmp_path / "runtime.json",
    )
    assert result.returncode == 2
    assert original.read_text() == "still live\n"


def test_install_rejects_a_snapshot_docs_manifest_that_does_not_match(tmp_path):
    snapshot = _snapshot(tmp_path)
    mirror = tmp_path / "live mirror"
    mirror.mkdir()
    original = mirror / "old.py"
    original.write_text("still live\n")
    (snapshot / ".docs-shipped.txt").write_text("docs/guide.md\n")

    result = _run(snapshot, mirror, "--expected-digest", _digest(snapshot))
    assert result.returncode == 2
    assert "snapshot docs do not match" in result.stderr
    assert original.read_text() == "still live\n"


def test_install_rejects_changed_snapshot_before_any_live_mutation(tmp_path):
    snapshot = _snapshot(tmp_path)
    expected = _digest(snapshot)
    mirror = tmp_path / "live mirror"
    mirror.mkdir()
    original = mirror / "old.py"
    original.write_text("still live\n")
    runtime_registry = tmp_path / "runtime" / "repo_review_registry.json"

    (snapshot / "module.py").write_text("VALUE = 'changed after verdict'\n")
    result = _run(
        snapshot,
        mirror,
        "--expected-digest",
        expected,
        "--runtime-registry",
        runtime_registry,
    )

    assert result.returncode == 2
    assert "retained snapshot digest" in result.stderr
    assert original.read_text() == "still live\n"
    assert not runtime_registry.exists()


@pytest.mark.parametrize("expected", ["", "xyz", "A" * 64, "0" * 63, "0" * 65])
def test_install_rejects_missing_or_malformed_verifier_digest_before_mutation(tmp_path, expected):
    snapshot = _snapshot(tmp_path)
    mirror = tmp_path / "live mirror"
    mirror.mkdir()
    original = mirror / "old.py"
    original.write_text("still live\n")
    args: list[str | Path] = [snapshot, mirror]
    if expected:
        args.extend(("--expected-digest", expected))

    result = _run(*args)

    assert result.returncode == 2
    assert original.read_text() == "still live\n"


def _live_outputs(tmp_path: Path) -> tuple[Path, Path]:
    mirror = tmp_path / "live mirror"
    (mirror / "docs").mkdir(parents=True)
    (mirror / "old.py").write_text("still live\n")
    (mirror / "docs" / "old.md").write_text("old guide\n")
    (mirror / ".docs-shipped.txt").write_text("docs/old.md\n")
    registry = tmp_path / "runtime.json"
    registry.write_text('{"old": true}\n')
    return mirror, registry


def _assert_live_outputs_unchanged(mirror: Path, registry: Path) -> None:
    assert (mirror / "old.py").read_text() == "still live\n"
    assert (mirror / "docs" / "old.md").read_text() == "old guide\n"
    assert (mirror / ".docs-shipped.txt").read_text() == "docs/old.md\n"
    assert not (mirror / "module.py").exists()
    assert registry.read_text() == '{"old": true}\n'


def test_payload_copy_failure_keeps_live_outputs_and_cleans_staging(tmp_path, monkeypatch):
    snapshot = _snapshot(tmp_path)
    mirror, registry = _live_outputs(tmp_path)
    expected = installer.snapshot_digest(snapshot)
    copy_entry = installer._copy_entry
    staging: list[Path] = []

    def fail_late(source: Path, destination: Path) -> None:
        if source == snapshot / "tests" / "fixtures" / "input.txt":
            assert destination.parents[2] != mirror
            staging.append(destination.parents[2])
            assert (staging[0] / "module.py").is_file()
            raise OSError("injected staging copy failure")
        copy_entry(source, destination)

    monkeypatch.setattr(installer, "_copy_entry", fail_late)
    with pytest.raises(OSError, match="injected staging copy failure"):
        installer.install(snapshot, mirror, expected, registry)

    _assert_live_outputs_unchanged(mirror, registry)
    assert len(staging) == 1
    assert not staging[0].exists()


@pytest.mark.parametrize("change", ["bytes", "mode", "link", "registry"])
def test_corrupt_copy_is_rejected_before_live_mutation(tmp_path, monkeypatch, change):
    snapshot = _snapshot(tmp_path)
    (snapshot / "scripts" / "tool-link.sh").symlink_to("nested/tool.sh")
    mirror, registry = _live_outputs(tmp_path)
    expected = installer.snapshot_digest(snapshot)
    copy_payload = installer._copy_payload

    def corrupt_copy(source: Path, destination: Path, entries: list[Path]) -> None:
        assert source == snapshot
        assert destination != mirror
        copy_payload(source, destination, entries)
        if change == "bytes":
            (destination / "module.py").write_text("changed while copying\n")
        elif change == "mode":
            (destination / "orchestrate.sh").chmod(0o644)
        elif change == "link":
            link = destination / "scripts" / "tool-link.sh"
            link.unlink()
            link.symlink_to("../orchestrate.sh")
        else:
            (destination / "repo_review_registry.json").write_text("not JSON\n")

    monkeypatch.setattr(installer, "_copy_payload", corrupt_copy)
    error = "Expecting value" if change == "registry" else "staged payload digest"
    with pytest.raises(ValueError, match=error):
        installer.install(snapshot, mirror, expected, registry)

    _assert_live_outputs_unchanged(mirror, registry)


def test_snapshot_change_during_staging_is_rejected_before_live_mutation(tmp_path, monkeypatch):
    snapshot = _snapshot(tmp_path)
    mirror, registry = _live_outputs(tmp_path)
    expected = installer.snapshot_digest(snapshot)
    copy_entry = installer._copy_entry

    def change_then_copy(source: Path, destination: Path) -> None:
        if source == snapshot / "module.py":
            source.write_text("changed after the initial digest check\n")
        copy_entry(source, destination)

    monkeypatch.setattr(installer, "_copy_entry", change_then_copy)
    with pytest.raises(ValueError, match="staged payload digest"):
        installer.install(snapshot, mirror, expected, registry)

    _assert_live_outputs_unchanged(mirror, registry)


def test_runtime_writes_during_preparation_are_preserved(tmp_path, monkeypatch):
    snapshot = _snapshot(tmp_path)
    mirror, registry = _live_outputs(tmp_path)
    (mirror / "docs" / "reports").mkdir()
    (mirror / "experiments").mkdir()
    expected = installer.snapshot_digest(snapshot)
    copy_payload = installer._copy_payload

    def write_then_copy(source: Path, destination: Path, entries: list[Path]) -> None:
        if source == snapshot:
            (mirror / "docs" / "reports" / "runtime.md").write_text("concurrent report\n")
            (mirror / "experiments" / ".last-ship-gate").write_text("concurrent marker\n")
        copy_payload(source, destination, entries)

    monkeypatch.setattr(installer, "_copy_payload", write_then_copy)
    assert installer.install(snapshot, mirror, expected, registry) == 0
    assert (mirror / "docs" / "reports" / "runtime.md").read_text() == "concurrent report\n"
    assert (mirror / "experiments" / ".last-ship-gate").read_text() == "concurrent marker\n"


def test_install_never_reopens_snapshot_after_staging_validation(tmp_path, monkeypatch):
    snapshot = _snapshot(tmp_path)
    mirror, registry = _live_outputs(tmp_path)
    expected = installer.snapshot_digest(snapshot)
    remove = installer._remove_owned_file
    displaced = tmp_path / "unavailable snapshot"

    def remove_live_entry(path: Path) -> None:
        if path == mirror / "old.py":
            snapshot.rename(displaced)
            (displaced / "repo_review_registry.json").write_text('{"changed": true}\n')
        remove(path)

    monkeypatch.setattr(installer, "_remove_owned_file", remove_live_entry)
    assert installer.install(snapshot, mirror, expected, registry) == 0
    assert not snapshot.exists()
    assert installer.snapshot_digest(mirror) == expected
    assert registry.read_text() == '{"repos": []}\n'


def test_runtime_writer_keeps_open_files_and_new_reports_during_publication(tmp_path, monkeypatch):
    snapshot = _snapshot(tmp_path)
    mirror, registry = _live_outputs(tmp_path)
    reports = mirror / "docs" / "reports"
    reports.mkdir()
    (mirror / "experiments").mkdir()
    report = reports / "runtime.md"
    marker = mirror / "experiments" / ".last-ship-gate"
    report.write_text("before report\n")
    marker.write_text("before marker\n")
    expected = installer.snapshot_digest(snapshot)
    publish_started = threading.Event()
    writes_finished = threading.Event()
    errors: list[BaseException] = []
    copy_entry = installer._copy_entry

    # Open handles before publication: copying runtime output to another directory
    # would strand these writes in the old inode even if the initial bytes survived.
    with report.open("a") as report_stream, marker.open("a") as marker_stream:

        def runtime_writer() -> None:
            try:
                assert publish_started.wait(10), "publisher did not reach live copy"
                report_stream.write("during report\n")
                report_stream.flush()
                marker_stream.write("during marker\n")
                marker_stream.flush()
                (reports / "new.md").write_text("created during publication\n")
            except BaseException as error:
                errors.append(error)
            finally:
                writes_finished.set()

        def copy_with_writer(source: Path, destination: Path) -> None:
            if destination == mirror / "module.py":
                # Live owned files have been removed; the new executable is not yet copied.
                assert not (mirror / "old.py").exists()
                assert not destination.exists()
                publish_started.set()
                assert writes_finished.wait(10), "runtime writer did not finish"
            copy_entry(source, destination)

        monkeypatch.setattr(installer, "_copy_entry", copy_with_writer)
        writer = threading.Thread(target=runtime_writer)
        writer.start()
        try:
            assert installer.install(snapshot, mirror, expected, registry) == 0
        finally:
            publish_started.set()
            writer.join(timeout=10)
        assert not writer.is_alive()
        assert not errors

    assert report.read_text() == "before report\nduring report\n"
    assert marker.read_text() == "before marker\nduring marker\n"
    assert (reports / "new.md").read_text() == "created during publication\n"
    assert not (mirror / "docs" / "old.md").exists()
    assert installer.snapshot_digest(mirror) == expected


@pytest.mark.parametrize(
    "relative",
    ["docs/old.md", "docs/guide.md", "experiments/hypotheses.json", "module.py", ".gitignore"],
)
def test_runtime_directory_at_owned_file_is_rejected_before_publication(tmp_path, relative):
    snapshot = _snapshot(tmp_path)
    mirror, registry = _live_outputs(tmp_path)
    collision = mirror / relative
    collision.unlink(missing_ok=True)
    collision.mkdir(parents=True)
    runtime_file = collision / "runtime.json"
    runtime_file.write_text("keep runtime bytes\n")

    result = _run(snapshot, mirror, "--expected-digest", _digest(snapshot))

    assert result.returncode == 2
    assert "would remove a runtime directory" in result.stderr
    assert runtime_file.read_text() == "keep runtime bytes\n"
    assert (mirror / "old.py").read_text() == "still live\n"
    assert (mirror / ".docs-shipped.txt").read_text() == "docs/old.md\n"
    assert registry.read_text() == '{"old": true}\n'


@pytest.mark.parametrize("relative", ["docs/briefs", "experiments", "data", "config"])
def test_runtime_storage_symlink_parent_is_rejected_before_publication(tmp_path, relative):
    snapshot = _snapshot(tmp_path)
    mirror, registry = _live_outputs(tmp_path)
    runtime = tmp_path / "runtime storage"
    runtime.mkdir()
    runtime_file = runtime / "runtime.json"
    runtime_file.write_text("keep runtime bytes\n")
    (mirror / relative).symlink_to(runtime, target_is_directory=True)

    result = _run(snapshot, mirror, "--expected-digest", _digest(snapshot))

    assert result.returncode == 2
    assert "deployment parent is not a real directory" in result.stderr
    _assert_live_outputs_unchanged(mirror, registry)
    assert runtime_file.read_text() == "keep runtime bytes\n"
    assert list(runtime.iterdir()) == [runtime_file]


def test_leaf_removal_never_recursively_removes_a_late_runtime_directory(tmp_path):
    path = tmp_path / "previously shipped.md"
    path.mkdir()
    report = path / "runtime.md"
    report.write_text("late runtime write\n")

    with pytest.raises(IsADirectoryError):
        installer._remove_owned_file(path)

    assert report.read_text() == "late runtime write\n"


def test_install_round_trips_symlinks_and_directory_permissions(tmp_path):
    snapshot = _snapshot(tmp_path)
    (snapshot / "scripts" / "tool-link.sh").symlink_to("nested/tool.sh")
    (snapshot / "tests" / "fixture-link").symlink_to("fixtures", target_is_directory=True)
    (snapshot / "scripts" / "nested").chmod(0o555)
    (snapshot / "docs" / "briefs").chmod(0o750)
    mirror = tmp_path / "live mirror"
    expected = _digest(snapshot)

    result = _run(snapshot, mirror, "--expected-digest", expected)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _digest(mirror) == expected
    assert os.readlink(mirror / "scripts" / "tool-link.sh") == "nested/tool.sh"
    assert os.readlink(mirror / "tests" / "fixture-link") == "fixtures"
    assert (mirror / "scripts" / "nested").stat().st_mode & 0o777 == 0o555
    assert (mirror / "docs" / "briefs").stat().st_mode & 0o777 == 0o750


def test_invalid_prior_manifest_is_rejected_before_any_live_removal(tmp_path):
    snapshot = _snapshot(tmp_path)
    mirror, registry = _live_outputs(tmp_path)
    manifest = mirror / ".docs-shipped.txt"
    manifest.write_text("docs/../old.py\n")

    result = _run(snapshot, mirror, "--expected-digest", _digest(snapshot))

    assert result.returncode == 2
    assert "unsafe shipped-doc path" in result.stderr
    assert (mirror / "old.py").read_text() == "still live\n"
    assert (mirror / "docs" / "old.md").read_text() == "old guide\n"
    assert manifest.read_text() == "docs/../old.py\n"
    assert registry.read_text() == '{"old": true}\n'


@pytest.mark.parametrize("tree", [*installer.REPLACED_TREES, *installer.MERGED_TREES])
def test_digest_rejects_structural_symlinks(tmp_path, tree):
    snapshot = _snapshot(tmp_path)
    (snapshot / tree).rename(tmp_path / "external tree")
    (snapshot / tree).symlink_to(tmp_path / "external tree", target_is_directory=True)

    result = _run(snapshot, "--digest")

    assert result.returncode == 2
    assert "snapshot tree is not a real directory" in result.stderr


@pytest.mark.parametrize("path", ["experiments/hypotheses.json", "docs/guide.md"])
def test_digest_rejects_entries_under_symlink_parents(tmp_path, path):
    snapshot = _snapshot(tmp_path)
    parent = (snapshot / path).parent
    parent.rename(tmp_path / "external directory")
    parent.symlink_to(tmp_path / "external directory", target_is_directory=True)

    result = _run(snapshot, "--digest")

    assert result.returncode == 2
    assert "symlink parent" in result.stderr or "not a real directory" in result.stderr


@pytest.mark.parametrize("path", ["data/feedback-snapshot.json", "extra.py"])
def test_digest_rejects_directories_in_file_ownership_positions(tmp_path, path):
    snapshot = _snapshot(tmp_path)
    (snapshot / path).mkdir()

    result = _run(snapshot, "--digest")

    assert result.returncode == 2
    assert "is a directory" in result.stderr


def test_digest_rejects_special_files_without_reading_them(tmp_path):
    snapshot = _snapshot(tmp_path)
    os.mkfifo(snapshot / "scripts" / "pipe")

    result = _run(snapshot, "--digest")

    assert result.returncode == 2
    assert "unsupported snapshot entry: scripts/pipe" in result.stderr


@pytest.mark.parametrize("target", ["absolute external", "absolute snapshot", "../../outside.sh"])
def test_digest_rejects_symlinks_that_would_read_outside_the_staged_payload(tmp_path, target):
    snapshot = _snapshot(tmp_path)
    if target == "absolute external":
        target = str(tmp_path / "outside.sh")
    elif target == "absolute snapshot":
        target = str(snapshot / "orchestrate.sh")
    (snapshot / "scripts" / "link.sh").symlink_to(target)

    result = _run(snapshot, "--digest")

    assert result.returncode == 2
    assert "snapshot symlink is not payload-relative" in result.stderr


@pytest.mark.parametrize("contents", ["docs//guide.md\n", "docs/guide.md\ndocs/guide.md\n"])
def test_digest_rejects_ambiguous_doc_manifest_paths(tmp_path, contents):
    snapshot = _snapshot(tmp_path)
    (snapshot / ".docs-shipped.txt").write_text(contents)

    result = _run(snapshot, "--digest")

    assert result.returncode == 2
    assert "unsafe shipped-doc path" in result.stderr
