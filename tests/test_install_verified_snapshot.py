from __future__ import annotations

import fcntl
import os
import stat
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from scripts import install_verified_snapshot as installer

import paths

INSTALLER = paths.REPO_ROOT / "scripts" / "install_verified_snapshot.py"
_LOG_ROTATION = "# --- Log rotation (every tick, cheap, fail-open)"
_GH_PREFLIGHT = "# ORCH-ANCHOR: gh-auth-preflight"
_HEARTBEAT_EXPORT = "# ORCH-ANCHOR: heartbeat-export"


def _orchestrate_tick_prologue() -> str:
    """Production tick preamble through mirror_reader, skipping log rotation only.

    mirror_reader re-entry moved below gh-auth-preflight (after log rotation); replay
    tests must still execute the same reader exclusion the launchd tick uses.
    """
    text = (paths.REPO_ROOT / "orchestrate.sh").read_text()
    head, _rest = text.split(_LOG_ROTATION, 1)
    tail = text.split(_GH_PREFLIGHT, 1)[1]
    reader_block = _GH_PREFLIGHT + tail.split(_HEARTBEAT_EXPORT, 1)[0]
    return head + reader_block


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


@pytest.mark.parametrize("tree", ["snapshot", "mirror"])
@pytest.mark.parametrize(
    "relative,parent_alias",
    [
        ("module.py", False),
        ("module.py", True),
        ("docs/reports/runtime.json", False),
        ("docs/reports/runtime.json", True),
        (".", False),
    ],
)
def test_runtime_registry_cannot_overwrite_deployment_or_runtime_mirror_content(
    tmp_path, tree, parent_alias, relative
):
    snapshot = _snapshot(tmp_path)
    mirror, registry = _live_outputs(tmp_path)
    report = mirror / "docs" / "reports" / "runtime.json"
    report.parent.mkdir()
    report.write_text('{"runtime": true}\n')
    root = snapshot if tree == "snapshot" else mirror
    if parent_alias:
        alias = tmp_path / "registry parent alias"
        alias.symlink_to(root, target_is_directory=True)
        root = alias
    # module.py catches a post-digest deployment overwrite; reports catch runtime ownership.
    destination = root / relative
    before_digest = _digest(snapshot)

    result = _run(
        snapshot,
        mirror,
        "--expected-digest",
        before_digest,
        "--runtime-registry",
        destination,
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert "runtime registry must be outside" in result.stderr
    _assert_live_outputs_unchanged(mirror, registry)
    assert report.read_text() == '{"runtime": true}\n'
    assert _digest(snapshot) == before_digest


def test_runtime_registry_replaces_a_leaf_symlink_without_following_its_target(tmp_path):
    snapshot = _snapshot(tmp_path)
    mirror, registry = _live_outputs(tmp_path)
    expected = _digest(snapshot)
    registry.unlink()
    registry.symlink_to(snapshot / "module.py")

    result = _run(snapshot, mirror, "--expected-digest", expected, "--runtime-registry", registry)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not registry.is_symlink()
    assert registry.read_text() == '{"repos": []}\n'
    assert _digest(snapshot) == expected
    assert _digest(mirror) == expected


def test_runtime_registry_uses_the_validated_parent_if_its_alias_moves(tmp_path, monkeypatch):
    snapshot = _snapshot(tmp_path)
    mirror, registry = _live_outputs(tmp_path)
    separate = tmp_path / "separate runtime"
    separate.mkdir()
    alias = tmp_path / "runtime parent alias"
    alias.symlink_to(separate, target_is_directory=True)
    expected = installer.snapshot_digest(snapshot)
    copy_payload = installer._copy_payload

    def move_alias_then_copy(source, destination, entries):
        if source == snapshot:
            alias.unlink()
            alias.symlink_to(mirror, target_is_directory=True)
        copy_payload(source, destination, entries)

    monkeypatch.setattr(installer, "_copy_payload", move_alias_then_copy)
    assert installer.install(snapshot, mirror, expected, alias / "module.py") == 0
    assert (separate / "module.py").read_text() == '{"repos": []}\n'
    assert installer.snapshot_digest(mirror) == expected
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
    copy_payload = installer._copy_payload
    displaced = tmp_path / "unavailable snapshot"

    def copy_then_displace(source: Path, destination: Path, entries: list[Path]) -> None:
        copy_payload(source, destination, entries)
        if source == snapshot and not displaced.exists():
            snapshot.rename(displaced)
            (displaced / "repo_review_registry.json").write_text('{"changed": true}\n')

    monkeypatch.setattr(installer, "_copy_payload", copy_then_displace)
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
            if destination.name == "module.py" and ".next-" in destination.parent.name:
                # Generation publication builds on a staging tree; the live mirror stays intact.
                assert (mirror / "old.py").exists()
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
    "retired",
    [
        "experiments/hypotheses.json",
        "experiments/features.json",
        "experiments/repo_knowledge.json",
        "data/feedback-snapshot.json",
        "config/coverage-baseline.json",
    ],
)
def test_retiring_named_deployment_file_preserves_concurrent_runtime_writes(
    tmp_path, monkeypatch, retired
):
    snapshot = _snapshot(tmp_path)
    (snapshot / retired).unlink(missing_ok=True)
    mirror, registry = _live_outputs(tmp_path)
    retired_path = mirror / retired
    retired_path.parent.mkdir(exist_ok=True)
    retired_path.write_text("retired deployment bytes\n")
    marker = retired_path.parent / ".last-ship-gate"
    marker.write_text("before publication\n")
    expected = installer.snapshot_digest(snapshot)
    swap_started = threading.Event()
    writes_finished = threading.Event()
    errors: list[BaseException] = []
    with marker.open("a") as marker_stream:

        def runtime_writer() -> None:
            try:
                assert swap_started.wait(10), "publisher did not reach generation swap"
                marker_stream.write("during publication\n")
                marker_stream.flush()
            except BaseException as error:
                errors.append(error)
            finally:
                writes_finished.set()

        path_rename = Path.rename

        def rename_with_writer(self, target):
            if self.parent == mirror.parent and self.name.startswith(mirror.name + ".next-"):
                swap_started.set()
                assert writes_finished.wait(10), "runtime writer did not finish"
            return path_rename(self, target)

        monkeypatch.setattr(Path, "rename", rename_with_writer)
        writer = threading.Thread(target=runtime_writer)
        writer.start()
        try:
            assert installer.install(snapshot, mirror, expected, registry) == 0
        finally:
            swap_started.set()
            writer.join(timeout=10)
        assert not writer.is_alive()
        assert not errors

    assert not retired_path.exists()
    assert marker.read_text() == "before publication\nduring publication\n"
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


@pytest.mark.parametrize("copy_fails", [False, True])
def test_runtime_registry_update_preserves_other_publishers_tempfile(
    tmp_path, monkeypatch, copy_fails
):
    snapshot = _snapshot(tmp_path)
    mirror, registry = _live_outputs(tmp_path)
    shared_temporary = registry.with_name(registry.name + ".tmp")
    shared_temporary.write_text("another publisher's pending registry\n")
    original_entries = set(registry.parent.iterdir())
    copy2 = installer.shutil.copy2
    observed = []

    def copy_registry(source, destination, **kwargs):
        destination = Path(destination)
        if destination.parent == registry.parent and destination != registry:
            observed.append(destination)
            if copy_fails:
                destination.write_text("partial registry\n")
                raise OSError("registry copy interrupted")
        return copy2(source, destination, **kwargs)

    monkeypatch.setattr(installer.shutil, "copy2", copy_registry)
    if copy_fails:
        with pytest.raises(OSError, match="registry copy interrupted"):
            installer.install(snapshot, mirror, installer.snapshot_digest(snapshot), registry)
        assert registry.read_text() == '{"old": true}\n'
    else:
        assert (
            installer.install(snapshot, mirror, installer.snapshot_digest(snapshot), registry) == 0
        )
        assert registry.read_text() == '{"repos": []}\n'

    assert shared_temporary.read_text() == "another publisher's pending registry\n"
    assert len(observed) == 1
    assert observed[0] != shared_temporary
    assert not observed[0].exists()
    publisher_state = {
        path
        for path in registry.parent.iterdir()
        if path.name.startswith(mirror.name + ".retired-")
        or path.name == f".{mirror.name}.publish.lock"
    }
    assert len(publisher_state) == 2
    assert set(registry.parent.iterdir()) - publisher_state == original_entries


@pytest.mark.parametrize(
    "failure",
    [None, "copy", "file_fsync", "replace", "directory_open", "directory_fsync"],
)
def test_runtime_registry_sync_order_cleanup_and_retry(tmp_path, monkeypatch, failure):
    snapshot = _snapshot(tmp_path)
    mirror, registry = _live_outputs(tmp_path)
    expected = installer.snapshot_digest(snapshot)
    events = []
    temporaries = []
    descriptors = []
    directory_fd = None
    fail_enabled = True
    copy2 = installer.shutil.copy2
    replace = Path.replace
    open_fd = installer.os.open
    close_fd = installer.os.close
    fsync = installer.os.fsync

    def observe(event):
        events.append(event)
        if fail_enabled and failure == event:
            raise OSError(f"injected {event} failure")

    def copy_registry(source, destination, **kwargs):
        destination = Path(destination)
        if destination.parent == registry.parent and destination != registry:
            temporaries.append(destination)
            observe("copy")
        return copy2(source, destination, **kwargs)

    def sync_descriptor(fd):
        descriptors.append(fd)
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            assert registry.read_bytes() == (snapshot / "repo_review_registry.json").read_bytes()
            observe("directory_fsync")
        else:
            assert registry.read_text() == '{"old": true}\n' or not fail_enabled
            assert (
                temporaries[-1].read_bytes()
                == (snapshot / "repo_review_registry.json").read_bytes()
            )
            observe("file_fsync")
        fsync(fd)

    def replace_registry(self, destination):
        if Path(destination) == registry:
            observe("replace")
        return replace(self, destination)

    def open_directory(path, flags, *args, **kwargs):
        nonlocal directory_fd
        if Path(path) == registry.parent:
            observe("directory_open")
            directory_fd = open_fd(path, flags, *args, **kwargs)
            descriptors.append(directory_fd)
            return directory_fd
        return open_fd(path, flags, *args, **kwargs)

    def close_descriptor(fd):
        nonlocal directory_fd
        if fd == directory_fd:
            events.append("directory_close")
            directory_fd = None
        return close_fd(fd)

    monkeypatch.setattr(installer.shutil, "copy2", copy_registry)
    monkeypatch.setattr(installer.os, "fsync", sync_descriptor)
    monkeypatch.setattr(installer.os, "open", open_directory)
    monkeypatch.setattr(installer.os, "close", close_descriptor)
    monkeypatch.setattr(Path, "replace", replace_registry)

    if failure is None:
        assert installer.install(snapshot, mirror, expected, registry) == 0
        assert events == [
            "copy",
            "file_fsync",
            "replace",
            "directory_open",
            "directory_fsync",
            "directory_close",
        ]
    else:
        with pytest.raises(OSError, match=f"injected {failure} failure"):
            installer.install(snapshot, mirror, expected, registry)
        order = ["copy", "file_fsync", "replace", "directory_open", "directory_fsync"]
        expected_events = order[: order.index(failure) + 1]
        if failure == "directory_fsync":
            expected_events.append("directory_close")
        assert events == expected_events

    # Registry errors are reported after mirror installation; a directory-sync error
    # leaves the new registry visible, with durability unconfirmed until a successful retry.
    assert installer.snapshot_digest(mirror) == expected
    published = failure in (None, "directory_open", "directory_fsync")
    assert registry.read_text() == ('{"repos": []}\n' if published else '{"old": true}\n')
    assert len(temporaries) == 1
    assert not temporaries[0].exists()
    assert not list(registry.parent.glob(f".{registry.name}.*.tmp"))
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)

    if failure is not None:
        fail_enabled = False
        assert installer.install(snapshot, mirror, expected, registry) == 0
        assert registry.read_bytes() == (snapshot / "repo_review_registry.json").read_bytes()
        assert installer.snapshot_digest(mirror) == expected
        assert all(not temporary.exists() for temporary in temporaries)


def test_runtime_writes_after_transfer_survive_switch_and_retry(tmp_path, monkeypatch):
    snapshot = _snapshot(tmp_path)
    mirror, registry = _live_outputs(tmp_path)
    reports = mirror / "docs" / "reports"
    reports.mkdir()
    report = reports / "runtime.md"
    report.write_text("before\n")
    (mirror / "experiments").mkdir()
    marker = mirror / "experiments" / ".last-ship-gate"
    marker.write_text("before\n")
    expected = installer.snapshot_digest(snapshot)
    transfer = installer._merge_runtime_content
    with report.open("a") as report_stream, marker.open("a") as marker_stream:

        def transfer_then_write(*args, **kwargs):
            with (mirror.parent / f".{mirror.name}.publish.lock").open("a") as rival:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(rival.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            transfer(*args, **kwargs)
            report_stream.write("after transfer\n")
            report_stream.flush()
            marker_stream.write("after transfer\n")
            marker_stream.flush()
            (reports / "late.md").write_text("late creation\n")

        monkeypatch.setattr(installer, "_merge_runtime_content", transfer_then_write)
        assert installer.install(snapshot, mirror, expected, registry) == 0
        report_stream.write("after publication\n")
        report_stream.flush()
        marker_stream.write("after publication\n")
        marker_stream.flush()
    assert (mirror / "docs/reports/runtime.md").read_text() == (
        "before\nafter transfer\nafter publication\n"
    )
    assert marker.read_text() == "before\nafter transfer\nafter publication\n"
    assert (mirror / "docs/reports/late.md").read_text() == "late creation\n"
    assert installer.snapshot_digest(mirror) == expected
    monkeypatch.setattr(installer, "_merge_runtime_content", transfer)
    assert installer.install(snapshot, mirror, expected, registry) == 0
    assert (mirror / "docs/reports/runtime.md").read_text().endswith("after publication\n")
    assert (mirror / "docs/reports/late.md").read_text() == "late creation\n"


def test_new_runtime_leaf_in_mixed_directory_survives_publication_and_retry(tmp_path, monkeypatch):
    """Runtime-only files created during publication in a mixed deployment/runtime tree."""
    snapshot = _snapshot(tmp_path)
    mirror, registry = _live_outputs(tmp_path)
    mixed = mirror / "experiments"
    mixed.mkdir()
    (mixed / "hypotheses.json").write_text("runtime copy\n")
    marker = mixed / ".last-ship-gate"
    marker.write_text("before\n")
    expected = installer.snapshot_digest(snapshot)
    transfer = installer._merge_runtime_content
    new_leaf = mixed / "runtime-spawn.json"

    def transfer_then_create(*args, **kwargs):
        transfer(*args, **kwargs)
        new_leaf.write_text('{"created": "during transfer"}\n')

    monkeypatch.setattr(installer, "_merge_runtime_content", transfer_then_create)
    assert installer.install(snapshot, mirror, expected, registry) == 0
    assert new_leaf.read_text() == '{"created": "during transfer"}\n'
    assert marker.read_text() == "before\n"
    assert (mixed / "hypotheses.json").read_text() == "{}\n"
    new_leaf.write_text('{"created": "after publication"}\n')
    monkeypatch.setattr(installer, "_merge_runtime_content", transfer)
    assert installer.install(snapshot, mirror, expected, registry) == 0
    assert new_leaf.read_text() == '{"created": "after publication"}\n'
    assert installer.snapshot_digest(mirror) == expected


def test_runtime_leaf_atomic_replacement_survives_publication_and_retry(tmp_path, monkeypatch):
    snapshot = _snapshot(tmp_path)
    mirror, registry = _live_outputs(tmp_path)
    (mirror / "experiments").mkdir()
    marker = mirror / "experiments" / ".last-ship-gate"
    marker.write_text("before\n")
    expected = installer.snapshot_digest(snapshot)
    transfer = installer._merge_runtime_content
    directory_fd = os.open(marker.parent, os.O_RDONLY)
    try:

        def replace_marker(value):
            fd = os.open(
                ".marker-next", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600, dir_fd=directory_fd
            )
            with os.fdopen(fd, "w") as stream:
                stream.write(value)
            os.replace(
                ".marker-next", ".last-ship-gate", src_dir_fd=directory_fd, dst_dir_fd=directory_fd
            )

        def transfer_then_replace(*args, **kwargs):
            transfer(*args, **kwargs)
            replace_marker("after transfer\n")

        monkeypatch.setattr(installer, "_merge_runtime_content", transfer_then_replace)
        assert installer.install(snapshot, mirror, expected, registry) == 0
        assert marker.read_text() == "after transfer\n"
        replace_marker("after publication\n")
        assert marker.read_text() == "after publication\n"
        monkeypatch.setattr(installer, "_merge_runtime_content", transfer)
        assert installer.install(snapshot, mirror, expected, registry) == 0
        replace_marker("after retry\n")
        assert marker.read_text() == "after retry\n"
        assert installer.snapshot_digest(mirror) == expected
    finally:
        os.close(directory_fd)


@pytest.mark.parametrize("failure", [None, "before", "after"])
def test_atomic_publication_has_no_missing_live_path(tmp_path, monkeypatch, failure):
    snapshot = _snapshot(tmp_path)
    mirror, registry = _live_outputs(tmp_path)
    (mirror / "module.py").write_text("VALUE = 'old'\n")
    report = mirror / "docs/reports/runtime.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("runtime survives\n")
    expected = installer.snapshot_digest(snapshot)
    rename = Path.rename
    observations = []

    def observe():
        assert mirror.is_dir(), "publisher exposed a missing live mirror"
        observations.append((mirror / "module.py").read_text())
        assert report.read_text() == "runtime survives\n"

    def observed_rename(self, target):
        result = rename(self, target)
        observe()
        return result

    monkeypatch.setattr(Path, "rename", observed_rename)
    if failure is not None:
        exchange = installer._exchange_directories

        def interrupted_exchange(left, right):
            observe()
            if failure == "before":
                raise OSError("injected pre-publication failure")
            exchange(left, right)
            observe()
            raise OSError("injected post-publication failure")

        monkeypatch.setattr(installer, "_exchange_directories", interrupted_exchange)
        with pytest.raises(OSError, match="injected"):
            installer.install(snapshot, mirror, expected, registry)
    else:
        installer.install(snapshot, mirror, expected, registry)
    observe()
    assert observations
    assert (mirror / "module.py").read_text() == (
        "VALUE = 'old'\n" if failure == "before" else "VALUE = 'verified'\n"
    )
    assert not list(mirror.parent.glob(mirror.name + ".next-*"))
    # Retry uses the same production publisher, preserving retained runtime backing.
    if failure is not None:
        monkeypatch.setattr(installer, "_exchange_directories", exchange)
    installer.install(snapshot, mirror, expected, registry)
    assert installer.snapshot_digest(mirror) == expected
    assert report.read_text() == "runtime survives\n"


@pytest.mark.parametrize("unavailable", ["platform", "kernel"])
def test_atomic_exchange_fails_closed_when_unavailable(tmp_path, monkeypatch, unavailable):
    import errno
    from unittest.mock import Mock

    snapshot = _snapshot(tmp_path)
    mirror, registry = _live_outputs(tmp_path)
    (mirror / "module.py").write_text("VALUE = 'old'\n")
    expected = installer.snapshot_digest(snapshot)
    if unavailable == "platform":
        monkeypatch.setattr(installer.sys, "platform", "unsupported")
    else:
        library = Mock()
        library.renamex_np.return_value = -1
        library.renameat2.return_value = -1
        monkeypatch.setattr(installer.ctypes, "CDLL", lambda *args, **kwargs: library)
        monkeypatch.setattr(installer.ctypes, "get_errno", lambda: errno.ENOTSUP)
    with pytest.raises(OSError) as error:
        installer.install(snapshot, mirror, expected, registry)
    assert error.value.errno == errno.ENOTSUP
    assert (mirror / "module.py").read_text() == "VALUE = 'old'\n"
    assert not list(mirror.parent.glob(mirror.name + ".next-*"))
    assert not list(mirror.parent.glob(mirror.name + ".retired-*"))


@pytest.mark.parametrize("active", [False, True])
def test_tick_reader_excludes_real_publisher_across_child_imports(tmp_path, monkeypatch, active):
    """Replay the production prologue and install(), with a child spanning publication."""
    import fcntl
    import os
    import threading

    snapshot = _snapshot(tmp_path)
    root = INSTALLER.parent.parent
    prologue = _orchestrate_tick_prologue()
    child = (
        "import sys; from pathlib import Path; import module, paths; "
        "print(module.VALUE, flush=True); sys.stdin.readline(); "
        "print((paths.MODULE_DIR / 'module.py').read_text().strip(), flush=True)"
    )
    import shlex

    script = prologue + f"python3 -c {shlex.quote(child)}\n"
    (snapshot / "orchestrate.sh").write_text(script)
    (snapshot / "mirror_reader.py").write_bytes((root / "src/mirror_reader.py").read_bytes())
    (snapshot / "paths.py").write_bytes((root / "src/paths.py").read_bytes())
    (snapshot / "module.py").write_text("VALUE = 'old'\n")
    watchdog_calls = tmp_path / "watchdog-calls"
    (snapshot / "tick_watchdog.py").write_text(
        "import sys; from pathlib import Path; "
        f"p = Path({str(watchdog_calls)!r}); "
        "p.open('a').write(sys.argv[sys.argv.index('--tick-pid') + 1] + '\\n')\n"
    )
    mirror = tmp_path / "live mirror"
    installer.install(snapshot, mirror, installer.snapshot_digest(snapshot))
    (snapshot / "module.py").write_text("VALUE = 'new'\n")
    digest = installer.snapshot_digest(snapshot)
    env = dict(
        os.environ, ORCH_DIR=str(mirror), HOME=str(tmp_path), ORCH_STATE_DIR=str(tmp_path / "state")
    )
    env.pop("ORCH_PUBLICATION_READER_FD", None)
    reader = subprocess.Popen(
        ["/bin/bash", str(mirror / "orchestrate.sh"), *(["--active"] if active else [])],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=mirror,
        env=env,
    )
    attempted = threading.Event()
    errors = []
    flock = fcntl.flock

    def observed_flock(fd, operation):
        if operation == fcntl.LOCK_EX:
            attempted.set()
        return flock(fd, operation)

    monkeypatch.setattr(installer.fcntl, "flock", observed_flock)

    def publish():
        try:
            installer.install(snapshot, mirror, digest)
        except BaseException as error:
            errors.append(error)

    publisher = threading.Thread(target=publish, daemon=True)
    try:
        lines = []
        while True:
            line = reader.stdout.readline()
            assert line, (lines, reader.stderr.read())
            lines.append(line.strip())
            if line.strip() == "old":
                break
        # A separate open description cannot acquire exclusive mode while the
        # reader spans imports. This asserts kernel exclusion without a timing guess.
        with (mirror.parent / f".{mirror.name}.publish.lock").open("a") as probe:
            try:
                flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                excluded = False
            except BlockingIOError:
                excluded = True
        publisher.start()
        assert attempted.wait(10), "production installer did not reach publication lock"
        if not excluded:
            # Negative control: let the unprotected publisher finish before the
            # child's second read, making the mixed-generation failure deterministic.
            publisher.join(15)
            assert not publisher.is_alive()
        reader.stdin.write("continue\n")
        reader.stdin.flush()
        output, error = reader.communicate(timeout=15)
        assert reader.returncode == 0, error
        if active:
            assert watchdog_calls.read_text().splitlines() == [str(reader.pid)]
        assert output.strip() == "VALUE = 'old'", "child crossed executable generations"
        publisher.join(15)
        assert not publisher.is_alive(), "publisher did not resume after reader exit"
        assert not errors
        assert (mirror / "module.py").read_text() == "VALUE = 'new'\n"
    finally:
        if reader.poll() is None:
            reader.kill()
            reader.communicate(timeout=10)
        if publisher.ident is not None:
            publisher.join(15)


def _reader_child_import_script() -> str:
    return (
        "import sys; from pathlib import Path; import module, paths; "
        "print(module.VALUE, flush=True); sys.stdin.readline(); "
        "print((paths.MODULE_DIR / 'module.py').read_text().strip(), flush=True)"
    )


def _run_reader_during_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    reader_cmd_factory: Callable[[Path], list[str]],
) -> None:
    """Shared witness: child spans publication and must stay on one generation."""
    import fcntl
    import os
    import threading

    snapshot = _snapshot(tmp_path)
    root = INSTALLER.parent.parent

    (snapshot / "mirror_reader.py").write_bytes((root / "src/mirror_reader.py").read_bytes())
    (snapshot / "paths.py").write_bytes((root / "src/paths.py").read_bytes())
    (snapshot / "module.py").write_text("VALUE = 'old'\n")
    mirror = tmp_path / "live mirror"
    installer.install(snapshot, mirror, installer.snapshot_digest(snapshot))
    (snapshot / "module.py").write_text("VALUE = 'new'\n")
    digest = installer.snapshot_digest(snapshot)
    env = dict(
        os.environ, ORCH_DIR=str(mirror), HOME=str(tmp_path), ORCH_STATE_DIR=str(tmp_path / "state")
    )
    env.pop("ORCH_PUBLICATION_READER_FD", None)
    reader_cmd = reader_cmd_factory(mirror)
    reader = subprocess.Popen(
        reader_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=mirror,
        env=env,
    )
    attempted = threading.Event()
    errors: list[BaseException] = []
    flock = fcntl.flock

    def observed_flock(fd, operation):
        if operation == fcntl.LOCK_EX:
            attempted.set()
        return flock(fd, operation)

    monkeypatch.setattr(installer.fcntl, "flock", observed_flock)

    def publish():
        try:
            installer.install(snapshot, mirror, digest)
        except BaseException as error:
            errors.append(error)

    publisher = threading.Thread(target=publish, daemon=True)
    try:
        lines = []
        while True:
            line = reader.stdout.readline()
            assert line, (lines, reader.stderr.read())
            lines.append(line.strip())
            if line.strip() == "old":
                break
        with (mirror.parent / f".{mirror.name}.publish.lock").open("a") as probe:
            try:
                flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                excluded = False
            except BlockingIOError:
                excluded = True
        publisher.start()
        assert attempted.wait(10), "production installer did not reach publication lock"
        if not excluded:
            publisher.join(15)
            assert not publisher.is_alive()
        reader.stdin.write("continue\n")
        reader.stdin.flush()
        output, error = reader.communicate(timeout=15)
        assert reader.returncode == 0, error
        assert output.strip() == "VALUE = 'old'", "child crossed executable generations"
        publisher.join(15)
        assert not publisher.is_alive(), "publisher did not resume after reader exit"
        assert not errors
        assert (mirror / "module.py").read_text() == "VALUE = 'new'\n"
    finally:
        if reader.poll() is None:
            reader.kill()
            reader.communicate(timeout=10)
        if publisher.ident is not None:
            publisher.join(15)


@pytest.mark.parametrize("guarded", [False, True], ids=["incumbent-entry", "guarded-entry"])
def test_standalone_python_reader_excludes_publisher_across_child_imports(
    tmp_path, monkeypatch, guarded
):
    """The same child observer crosses generations without the standalone entry guard."""
    root = INSTALLER.parent.parent
    child = _reader_child_import_script()
    mirror_reader = root / "src/mirror_reader.py"

    def reader_cmd_factory(mirror: Path) -> list[str]:
        command = ["python3", "-c", child]
        if guarded:
            command = ["python3", str(mirror_reader), "run", str(mirror), *command]
        return command

    if guarded:
        _run_reader_during_publication(tmp_path, monkeypatch, reader_cmd_factory=reader_cmd_factory)
    else:
        with pytest.raises(AssertionError, match="child crossed executable generations"):
            _run_reader_during_publication(
                tmp_path, monkeypatch, reader_cmd_factory=reader_cmd_factory
            )


def test_tick_reader_rejects_stale_or_wrong_mirror_lock(tmp_path, monkeypatch):
    import fcntl

    import mirror_reader

    root = tmp_path / "mirror"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv(mirror_reader.LOCK_ENV, "999999")
    assert not mirror_reader.inherited_lock(root)
    with mirror_reader.lock_path(other).open("a") as wrong:
        fcntl.flock(wrong.fileno(), fcntl.LOCK_SH)
        monkeypatch.setenv(mirror_reader.LOCK_ENV, str(wrong.fileno()))
        assert not mirror_reader.inherited_lock(root)
    with mirror_reader.lock_path(root).open("a") as correct:
        fcntl.flock(correct.fileno(), fcntl.LOCK_SH)
        monkeypatch.setenv(mirror_reader.LOCK_ENV, str(correct.fileno()))
        assert mirror_reader.inherited_lock(root)


@pytest.mark.parametrize("copy_result", ["success", "partial-error", "invalid-payload"])
def test_unverified_route_stages_copier_and_never_claims_verification(tmp_path, copy_result):
    snapshot = _snapshot(tmp_path)
    mirror, registry = _live_outputs(tmp_path)
    home = tmp_path / "home"
    (home / ".codex" / "orchestrator").mkdir(parents=True)
    live_registry = home / ".codex" / "orchestrator" / "repo_review_registry.json"
    live_registry.write_text("incumbent registry\n")
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    record = tmp_path / "copy-destinations"
    copier = tmp_path / "copier.sh"
    copier.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'printf \'%s\\n%s\\n\' "$HOME" "$ORCH_MIRROR" > "$COPY_RECORD"\n'
        'mkdir -p "$ORCH_MIRROR"\n'
        'cp -R "$1/." "$ORCH_MIRROR/"\n'
        # The real copier also writes this separate registry via HOME. Neither
        # this intermediate value nor a partial copy may reach live state.
        'echo scratch-registry > "$HOME/.codex/orchestrator/repo_review_registry.json"\n'
        '[[ "$COPY_RESULT" != partial-error ]] || exit 17\n'
        '[[ "$COPY_RESULT" != invalid-payload ]] || rm "$ORCH_MIRROR/orchestrate.sh"\n'
    )
    result = subprocess.run(
        [
            "bash",
            str(paths.REPO_ROOT / "scripts/publish_unverified_snapshot.sh"),
            str(snapshot),
            str(mirror),
        ],
        env={
            **os.environ,
            "HOME": str(home),
            "TMPDIR": str(temporary),
            "ORCH_SYNC_SCRIPT": str(copier),
            "COPY_RECORD": str(record),
            "COPY_RESULT": copy_result,
        },
        capture_output=True,
        text=True,
    )
    copied_home, copied_mirror = map(Path, record.read_text().splitlines())
    assert copied_home.is_relative_to(temporary)
    assert copied_mirror.is_relative_to(temporary)
    assert not copied_home.exists() and not copied_mirror.exists()
    assert list(temporary.iterdir()) == []
    if copy_result == "success":
        assert result.returncode == 0, result.stdout + result.stderr
        assert "installed UNVERIFIED snapshot" in result.stdout
        assert "installed verified snapshot" not in result.stdout
        assert installer.snapshot_digest(mirror) == installer.snapshot_digest(snapshot)
        assert live_registry.read_text() == '{"repos": []}\n'
    else:
        assert result.returncode == 2, result.stdout + result.stderr
        assert (mirror / "old.py").read_text() == "still live\n"
        assert live_registry.read_text() == "incumbent registry\n"
    assert registry.read_text() == '{"old": true}\n'


@pytest.mark.parametrize("guarded", [False, True])
@pytest.mark.parametrize("copy_failure", [False, True])
def test_direct_copier_guard_keeps_live_tree_available(tmp_path, guarded, copy_failure):
    """The same destructive copier loses a live file only without the entry guard."""
    snapshot = _snapshot(tmp_path)
    for name in ("incumbent_copy_guard.sh", "publish_unverified_snapshot.sh"):
        (snapshot / "scripts" / name).write_bytes((paths.REPO_ROOT / "scripts" / name).read_bytes())
    mirror, _registry = _live_outputs(tmp_path)
    home = tmp_path / "home"
    runtime = home / ".codex/orchestrator/repo_review_registry.json"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("old registry\n")
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    record = tmp_path / "reader-observation"
    copier = tmp_path / "direct-copier.sh"
    copier.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'SRC="$1"\nMIRROR="$ORCH_MIRROR"\n'
        + ('source "$SRC/scripts/incumbent_copy_guard.sh"\n' if guarded else "")
        + 'mkdir -p "$MIRROR"\n'
        + 'find "$MIRROR" -maxdepth 1 -name "*.py" -delete\n'
        + 'if [[ -f "$LIVE_MIRROR/old.py" ]]; then echo present; else echo missing; fi'
        + ' > "$READER_RECORD"\n'
        + '[[ "$COPY_FAILURE" != 1 ]] || exit 17\n'
        + 'cp -R "$SRC/." "$MIRROR/"\n'
    )
    result = subprocess.run(
        ["bash", str(copier), str(snapshot)],
        env={
            **os.environ,
            "HOME": str(home),
            "TMPDIR": str(temporary),
            "ORCH_SYNC_SCRIPT": str(copier),
            "ORCH_MIRROR": str(mirror),
            # An accidental override must not grant private-copy status to live HOME/MIRROR.
            "ORCH_PRIVATE_COPY_ROOT": str(temporary),
            "LIVE_MIRROR": str(mirror),
            "READER_RECORD": str(record),
            "COPY_FAILURE": "1" if copy_failure else "0",
        },
        capture_output=True,
        text=True,
    )
    assert record.read_text().strip() == ("present" if guarded else "missing")
    assert list(temporary.iterdir()) == []
    if copy_failure:
        assert result.returncode == (2 if guarded else 17), result.stdout + result.stderr
        assert (mirror / "old.py").exists() == guarded
        assert runtime.read_text() == "old registry\n"
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert (mirror / "module.py").read_text() == "VALUE = 'verified'\n"
        if guarded:
            assert "installed UNVERIFIED snapshot" in result.stdout
            assert "installed verified snapshot" not in result.stdout
            assert installer.snapshot_digest(mirror) == installer.snapshot_digest(snapshot)
            assert runtime.read_text() == '{"repos": []}\n'


def test_overlapping_publishers_serialize_on_exclusive_lock(tmp_path, monkeypatch):
    """The second publisher blocks until the first completes the atomic switch."""
    import shutil
    import threading

    snapshot_old = _snapshot(tmp_path)
    snapshot_new = tmp_path / "verified snapshot new"
    shutil.copytree(snapshot_old, snapshot_new)
    (snapshot_new / "module.py").write_text("VALUE = 'new'\n")
    mirror, registry = _live_outputs(tmp_path)
    (mirror / "module.py").write_text("VALUE = 'old'\n")
    digest_old = installer.snapshot_digest(snapshot_old)
    digest_new = installer.snapshot_digest(snapshot_new)
    installer.install(snapshot_old, mirror, digest_old, registry)

    publishing = threading.Event()
    release_first = threading.Event()
    publish_generation = installer._publish_generation

    def slow_publish(*args, **kwargs):
        publishing.set()
        assert release_first.wait(15), "first publisher released before overlap was observed"
        return publish_generation(*args, **kwargs)

    monkeypatch.setattr(installer, "_publish_generation", slow_publish)
    errors: list[BaseException] = []

    def publish_new():
        try:
            installer.install(snapshot_new, mirror, digest_new, registry)
        except BaseException as error:
            errors.append(error)

    def publish_again():
        try:
            installer.install(snapshot_new, mirror, digest_new, registry)
        except BaseException as error:
            errors.append(error)

    first = threading.Thread(target=publish_new, daemon=True)
    second = threading.Thread(target=publish_again, daemon=True)
    first.start()
    assert publishing.wait(15), "first publisher did not reach generation publication"
    second.start()
    second.join(0.5)
    assert second.is_alive(), "second publisher finished before the first released the lock"
    assert (mirror / "module.py").read_text() == "VALUE = 'verified'\n"
    release_first.set()
    first.join(30)
    second.join(30)
    assert not errors
    assert (mirror / "module.py").read_text() == "VALUE = 'new'\n"
    assert installer.snapshot_digest(mirror) == digest_new
