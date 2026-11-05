"""Pure, runtime-pinned Source snapshot ownership and digest contract."""

from __future__ import annotations

import hashlib
import os
import stat
import struct
from pathlib import Path

REPLACED_TREES = ("tests", "scripts", ".github")
MERGED_TREES = ("docs",)
OWNED_FILES = (
    ".docs-shipped.txt",
    ".gitignore",
    ".verify-floor.json",
    ".coveragerc",
    "pyproject.toml",
    "ruff.toml",
    "CLAUDE.md",
    "AGENTS.md",
    "ORCHESTRATOR.md",
    "IMPROVEMENT_BACKLOG.md",
    "orchestrator-loop.svg",
    "repo_review_registry.json",
    "experiments/hypotheses.json",
    "experiments/features.json",
    "experiments/repo_knowledge.json",
    "data/feedback-snapshot.json",
    "config/coverage-baseline.json",
    "config/test-shards.json",
    "config/model_epochs.json",
    "closer_role_selection.json",
    "tools/make_test_shards.py",
)
GENERATED_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", "htmlcov"}


def _walk_error(error: OSError) -> None:
    raise error


def _tree_entries(root: Path, relative: str) -> list[Path]:
    base = root / relative
    if not base.is_dir() or base.is_symlink():
        return []
    entries = [Path(relative)]
    for directory, dirs, files in os.walk(base, followlinks=False, onerror=_walk_error):
        here = Path(directory)
        dirs[:] = sorted(n for n in dirs if n not in GENERATED_DIRS)
        entries.extend((here / n).relative_to(root) for n in dirs)
        entries.extend(
            (here / n).relative_to(root)
            for n in sorted(files)
            if not n.startswith(".coverage") and not n.endswith((".pyc", ".pyo"))
        )
    return entries


def _shipped_docs(path: Path) -> list[Path]:
    if path.is_symlink():
        raise ValueError(f"shipped-doc manifest must not be a symlink: {path}")
    if not path.exists():
        return []
    if not path.is_file():
        raise ValueError(f"shipped-doc manifest is not a file: {path}")
    paths: list[Path] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        relative = Path(line)
        if (
            not line.startswith("docs/")
            or relative.is_absolute()
            or ".." in relative.parts
            or str(relative) != line
            or relative in paths
        ):
            raise ValueError(f"unsafe shipped-doc path in {path}: {line!r}")
        paths.append(relative)
    return paths


def owned_entries(snapshot: Path) -> list[Path]:
    entries: set[Path] = set()
    for pattern in ("*.py", "*.sh"):
        entries.update(p.relative_to(snapshot) for p in snapshot.glob(pattern))
    for tree in REPLACED_TREES:
        entries.update(_tree_entries(snapshot, tree))
    for tree in MERGED_TREES:
        for relative in _shipped_docs(snapshot / f".{tree}-shipped.txt"):
            entries.add(relative)
            parent = relative.parent
            while parent != Path("."):
                entries.add(parent)
                parent = parent.parent
    entries.update(
        Path(x) for x in OWNED_FILES if (snapshot / x).exists() or (snapshot / x).is_symlink()
    )
    return sorted(entries, key=lambda p: (len(p.parts), os.fsencode(str(p))))


def snapshot_digest(snapshot: Path, *, installer_name: str = "install_verified_snapshot.py") -> str:
    snapshot = snapshot.resolve()
    _validate_snapshot(snapshot, installer_name=installer_name)
    digest = hashlib.sha256()
    for relative in owned_entries(snapshot):
        path = snapshot / relative
        meta = path.lstat()
        for field in (os.fsencode(relative), oct(stat.S_IMODE(meta.st_mode)).encode()):
            digest.update(struct.pack(">Q", len(field)))
            digest.update(field)
        if path.is_symlink():
            kind, payload = b"symlink", os.fsencode(os.readlink(path))
        elif path.is_dir():
            kind, payload = b"directory", None
        elif path.is_file():
            kind, payload = b"file", path.read_bytes()
        else:
            raise ValueError(f"unsupported snapshot entry: {relative}")
        digest.update(struct.pack(">Q", len(kind)))
        digest.update(kind)
        if payload is not None:
            digest.update(struct.pack(">Q", len(payload)))
            digest.update(payload)
    return digest.hexdigest()


def _validate_snapshot(
    snapshot: Path, *, installer_name: str = "install_verified_snapshot.py"
) -> None:
    if not snapshot.is_dir():
        raise ValueError(f"snapshot is not a directory: {snapshot}")
    if not (snapshot / "orchestrate.sh").is_file():
        raise ValueError(f"snapshot has no orchestrate.sh: {snapshot}")
    if not list(snapshot.glob("*.py")):
        raise ValueError(f"snapshot has no flat Python modules: {snapshot}")
    installer = snapshot / "scripts" / installer_name
    if not installer.is_file():
        raise ValueError(f"snapshot has no verified installer: {installer}")
    for tree in (*REPLACED_TREES, *MERGED_TREES):
        path = snapshot / tree
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ValueError(f"snapshot tree is not a real directory: {tree}")
    for relative in owned_entries(snapshot):
        path = snapshot / relative
        if any((snapshot / parent).is_symlink() for parent in relative.parents):
            raise ValueError(f"snapshot entry has a symlink parent: {relative}")
        mode = path.lstat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
            raise ValueError(f"unsupported snapshot entry: {relative}")
        if stat.S_ISLNK(mode):
            target = Path(os.readlink(path))
            if target.is_absolute() or not path.resolve().is_relative_to(snapshot):
                raise ValueError(f"snapshot symlink is not payload-relative: {relative}")
        if str(relative) in OWNED_FILES and stat.S_ISDIR(mode):
            raise ValueError(f"snapshot owned file is a directory: {relative}")
        if len(relative.parts) == 1 and relative.suffix in (".py", ".sh") and stat.S_ISDIR(mode):
            raise ValueError(f"snapshot module or script is a directory: {relative}")
