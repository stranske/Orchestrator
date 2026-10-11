"""The lint baseline's scratch tree must preserve tracked binary files without edits."""

import subprocess
from pathlib import Path

import pytest

from scripts import ci_lint_baseline as baseline


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            *args,
        ],
        check=True,
        capture_output=True,
    )


@pytest.mark.parametrize(
    "payload", [b"\x00\x8b\xff\xfe\r\ntracked binary\x00", b"line 1\r\nline 2\r\n"]
)
def test_scratch_copy_preserves_binary_head_and_leaves_checkout_untouched(
    tmp_path, monkeypatch, payload
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "asset.bin").write_bytes(payload)
    (repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "asset.bin", "module.py")
    _git(repo, "commit", "-qm", "binary fixture")
    (repo / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    (repo / "untracked.py").write_text("VALUE = 3\n", encoding="utf-8")
    monkeypatch.setattr(baseline, "REPO", repo)

    with baseline._scratch_copy() as scratch:
        scratch_path = Path(scratch)
        assert (scratch_path / "asset.bin").read_bytes() == payload
        assert (scratch_path / "module.py").read_text(encoding="utf-8") == "VALUE = 1\n"
        assert not (scratch_path / "untracked.py").exists()
        (scratch_path / "module.py").write_text("VALUE = 4\n", encoding="utf-8")

    assert not scratch_path.exists()
    assert (repo / "asset.bin").read_bytes() == payload
    assert (repo / "module.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert (repo / "untracked.py").read_text(encoding="utf-8") == "VALUE = 3\n"


def test_scratch_copy_reports_archive_failure_and_cleans_up(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    monkeypatch.setattr(baseline, "REPO", repo)
    monkeypatch.setattr(baseline.tempfile, "tempdir", str(tmp_path))

    with pytest.raises(SystemExit, match="git archive HEAD failed:"):
        baseline._scratch_copy()

    assert not list(tmp_path.glob("ci-lint-baseline-*"))
