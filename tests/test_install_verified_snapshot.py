from __future__ import annotations

import os
import subprocess
from pathlib import Path

import paths

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

    result = _run(snapshot, mirror, "--runtime-registry", registry)
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
    result = _run(snapshot, snapshot)
    assert result.returncode == 2
    assert "snapshot and mirror must be separate trees" in result.stderr


def test_install_validates_every_external_output_before_mutating_the_mirror(tmp_path):
    snapshot = _snapshot(tmp_path)
    mirror = tmp_path / "live mirror"
    mirror.mkdir()
    original = mirror / "old.py"
    original.write_text("still live\n")
    (snapshot / "repo_review_registry.json").write_text("not json\n")

    result = _run(snapshot, mirror, "--runtime-registry", tmp_path / "runtime.json")
    assert result.returncode == 2
    assert original.read_text() == "still live\n"


def test_install_rejects_a_snapshot_docs_manifest_that_does_not_match(tmp_path):
    snapshot = _snapshot(tmp_path)
    mirror = tmp_path / "live mirror"
    mirror.mkdir()
    original = mirror / "old.py"
    original.write_text("still live\n")
    (snapshot / ".docs-shipped.txt").write_text("docs/guide.md\n")

    result = _run(snapshot, mirror)
    assert result.returncode == 2
    assert "snapshot docs do not match" in result.stderr
    assert original.read_text() == "still live\n"
