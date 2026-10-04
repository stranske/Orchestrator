#!/usr/bin/env python3
"""Install only the deployment-owned bytes from a verified flat-mirror snapshot.

The ordinary copy script builds the snapshot.  This installer deliberately does not read the
mutable source checkout or fetch the Workflows registry again: the bytes it installs are the bytes
that ``verify_before_sync.sh`` hashed and verified.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import sys
import tempfile
import uuid
from pathlib import Path

# These trees and root-level *.py/*.sh files are deployment-owned in full. Merged
# trees own only their manifest leaves; named files own no siblings or parent trees.
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
    "IMPROVEMENT_BACKLOG.md",
    "repo_review_registry.json",
    "experiments/hypotheses.json",
    "experiments/features.json",
    "experiments/repo_knowledge.json",
    "data/feedback-snapshot.json",
    "config/coverage-baseline.json",
)
GENERATED_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", "htmlcov"}


def _walk_error(error: OSError) -> None:
    raise error


def _add_digest_field(digest: hashlib._Hash, value: bytes) -> None:
    digest.update(struct.pack(">Q", len(value)))
    digest.update(value)


def _tree_entries(root: Path, relative: str) -> list[Path]:
    base = root / relative
    if not base.is_dir() or base.is_symlink():
        return []
    entries: list[Path] = [Path(relative)]
    for directory, dirnames, filenames in os.walk(base, followlinks=False, onerror=_walk_error):
        here = Path(directory)
        dirnames[:] = sorted(name for name in dirnames if name not in GENERATED_DIRS)
        for name in dirnames:
            entries.append((here / name).relative_to(root))
        for name in sorted(filenames):
            if name.startswith(".coverage") or name.endswith((".pyc", ".pyo")):
                continue
            entries.append((here / name).relative_to(root))
    return entries


def owned_entries(snapshot: Path) -> list[Path]:
    """Return the exact source-owned paths the legacy copier deploys, in stable order."""

    entries: set[Path] = set()
    for pattern in ("*.py", "*.sh"):
        entries.update(path.relative_to(snapshot) for path in snapshot.glob(pattern))
    for tree in REPLACED_TREES:
        entries.update(_tree_entries(snapshot, tree))
    for tree in MERGED_TREES:
        for relative in _shipped_docs(snapshot / f".{tree}-shipped.txt"):
            entries.add(relative)
            parent = relative.parent
            while parent != Path("."):
                entries.add(parent)
                parent = parent.parent
    for owned_file in OWNED_FILES:
        path = snapshot / owned_file
        if path.exists() or path.is_symlink():
            entries.add(Path(owned_file))
    return sorted(entries, key=lambda path: (len(path.parts), os.fsencode(str(path))))


def snapshot_digest(snapshot: Path) -> str:
    """Hash path, kind, permissions, symlink target, and bytes for every deployed entry."""

    snapshot = snapshot.resolve()
    _validate_snapshot(snapshot)
    digest = hashlib.sha256()
    for relative in owned_entries(snapshot):
        path = snapshot / relative
        metadata = path.lstat()
        _add_digest_field(digest, os.fsencode(relative))
        _add_digest_field(digest, oct(stat.S_IMODE(metadata.st_mode)).encode())
        if path.is_symlink():
            _add_digest_field(digest, b"symlink")
            _add_digest_field(digest, os.fsencode(os.readlink(path)))
        elif path.is_dir():
            _add_digest_field(digest, b"directory")
        elif path.is_file():
            _add_digest_field(digest, b"file")
            _add_digest_field(digest, path.read_bytes())
        else:
            raise ValueError(f"unsupported snapshot entry: {relative}")
    return digest.hexdigest()


def _validate_snapshot(snapshot: Path) -> None:
    snapshot = snapshot.resolve()
    if not snapshot.is_dir():
        raise ValueError(f"snapshot is not a directory: {snapshot}")
    if not (snapshot / "orchestrate.sh").is_file():
        raise ValueError(f"snapshot has no orchestrate.sh: {snapshot}")
    if not list(snapshot.glob("*.py")):
        raise ValueError(f"snapshot has no flat Python modules: {snapshot}")
    installer = snapshot / "scripts" / Path(__file__).name
    if not installer.is_file():
        raise ValueError(f"snapshot has no verified installer: {installer}")
    # A structural symlink would make enumeration omit a tree or copy bytes outside the
    # payload. Leaf symlinks are retained as links, including their exact target metadata.
    for tree in (*REPLACED_TREES, *MERGED_TREES):
        path = snapshot / tree
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ValueError(f"snapshot tree is not a real directory: {tree}")
    for relative in owned_entries(snapshot):
        path = snapshot / relative
        for parent in relative.parents:
            if (snapshot / parent).is_symlink():
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
        if len(relative.parts) == 1 and relative.suffix in (".py", ".sh"):
            if stat.S_ISDIR(mode):
                raise ValueError(f"snapshot module or script is a directory: {relative}")


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _remove_owned_file(path: Path) -> None:
    """Remove a deployment leaf without recursively deleting runtime-owned children."""

    # unlink refuses a directory, including one created after the ownership preflight.
    # A symlink is removed as a leaf without following its runtime-owned target.
    if path.is_dir() and not path.is_symlink():
        raise IsADirectoryError(f"refusing to remove runtime directory: {path}")
    path.unlink(missing_ok=True)


def _validate_mirror_ownership(
    payload: Path, mirror: Path, entries: list[Path], prior_docs: list[Path]
) -> None:
    """Fail before publication if a deployment leaf would consume a runtime directory."""

    leaves = set(prior_docs) | {Path(name) for name in OWNED_FILES}
    leaves.update(
        path.relative_to(mirror) for pattern in ("*.py", "*.sh") for path in mirror.glob(pattern)
    )
    directories: set[Path] = set()
    for relative in entries:
        if relative.parts[0] in REPLACED_TREES:
            continue
        source = payload / relative
        if source.is_dir() and not source.is_symlink():
            directories.add(relative)
        else:
            leaves.add(relative)
    for relative in leaves:
        directories.update(parent for parent in relative.parents if parent != Path("."))
    # Do not follow mirror-local symlinks into runtime storage when installing or
    # removing owned files. Check shallow parents before inspecting any child.
    for relative in sorted(directories, key=lambda path: (len(path.parts), str(path))):
        path = mirror / relative
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ValueError(f"deployment parent is not a real directory: {relative}")
    for relative in leaves:
        path = mirror / relative
        if path.is_dir() and not path.is_symlink():
            raise ValueError(f"deployment file would remove a runtime directory: {relative}")


def _copy_entry(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        _remove_owned_file(destination)
        destination.symlink_to(os.readlink(source))
        shutil.copystat(source, destination, follow_symlinks=False)
    elif source.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
    else:
        _remove_owned_file(destination)
        shutil.copy2(source, destination, follow_symlinks=False)


def _copy_payload(source: Path, destination: Path, entries: list[Path]) -> None:
    for relative in entries:
        _copy_entry(source / relative, destination / relative)
    # Apply directory permissions last, so a read-only shipped directory can still be built.
    for relative in reversed(entries):
        path = source / relative
        if path.is_dir() and not path.is_symlink():
            shutil.copystat(path, destination / relative, follow_symlinks=False)


def _shipped_docs(manifest: Path) -> list[Path]:
    """Read the copier's prior docs manifest without permitting path traversal."""

    if manifest.is_symlink():
        raise ValueError(f"shipped-doc manifest must not be a symlink: {manifest}")
    if not manifest.exists():
        return []
    if not manifest.is_file():
        raise ValueError(f"shipped-doc manifest is not a file: {manifest}")
    paths: list[Path] = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        relative = Path(line)
        if (
            not line.startswith("docs/")
            or relative.is_absolute()
            or ".." in relative.parts
            or str(relative) != line
            or relative in paths
        ):
            raise ValueError(f"unsafe shipped-doc path in {manifest}: {line!r}")
        paths.append(relative)
    return paths


def _validate_payload(payload: Path) -> None:
    """Validate the complete copy, before any deployment-owned live entry is removed."""

    _validate_snapshot(payload)
    registry = payload / "repo_review_registry.json"
    if registry.is_file():
        json.loads(registry.read_text(encoding="utf-8"))
    snapshot_docs = _shipped_docs(payload / ".docs-shipped.txt")
    if set(snapshot_docs) != {
        path.relative_to(payload)
        for path in (payload / "docs").rglob("*")
        if path.is_file() or path.is_symlink()
    }:
        raise ValueError("snapshot docs do not match .docs-shipped.txt")


def _deployment_owned_relative(relative: Path, entries: set[Path]) -> bool:
    if relative in entries:
        return True
    if relative.parts and relative.parts[0] in REPLACED_TREES:
        return True
    if len(relative.parts) == 1 and relative.suffix in (".py", ".sh"):
        return True
    if str(relative) in OWNED_FILES:
        return True
    return False


def _merge_runtime_content(
    live: Path,
    staging: Path,
    entries: set[Path],
    prior_docs: list[Path],
    retained: Path,
) -> None:
    """Bring mirror-local runtime output into a staged generation without touching deployment leaves."""

    if not live.exists():
        return
    retired_shipped_docs = set(prior_docs) - set(_shipped_docs(staging / ".docs-shipped.txt"))
    shared_directories: list[Path] = []
    protected = (
        entries
        | set(prior_docs)
        | {Path(name) for name in OWNED_FILES}
        | {Path(name) for name in MERGED_TREES}
    )
    for path in sorted(
        live.rglob("*"), key=lambda candidate: (len(candidate.parts), str(candidate))
    ):
        relative = path.relative_to(live)
        if any(parent == relative or parent in relative.parents for parent in shared_directories):
            continue
        if path.is_dir() and not path.is_symlink():
            if not _deployment_owned_relative(relative, entries) and not any(
                relative == owned or relative in owned.parents for owned in protected
            ):
                destination = staging / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.symlink_to(retained / relative, target_is_directory=True)
                shared_directories.append(relative)
            continue
        if not path.is_file() and not path.is_symlink():
            continue
        relative = path.relative_to(live)
        if _deployment_owned_relative(relative, entries):
            continue
        if relative in retired_shipped_docs:
            continue
        destination = staging / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            if destination.exists():
                destination.unlink()
            destination.symlink_to(os.readlink(path))
            shutil.copystat(path, destination, follow_symlinks=False)
        else:
            # Follow the retained directory entry, not just its current inode:
            # writers may atomically replace a runtime marker via an open parent
            # directory descriptor after transfer or even after publication.
            # The old directory lands at retained in the same atomic exchange.
            destination.symlink_to(retained / relative)


def _exchange_directories(left: Path, right: Path) -> None:
    """Exchange two existing directory entries in one kernel operation.

    A two-rename fallback exposes a missing live path. Unsupported platforms or
    filesystems therefore fail before changing the live generation.
    """

    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        exchange = getattr(library, "renamex_np", None)
        if exchange is None:
            raise OSError(errno.ENOTSUP, "atomic directory exchange is unavailable")
        exchange.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        exchange.restype = ctypes.c_int
        result = exchange(os.fsencode(left), os.fsencode(right), 0x00000002)  # RENAME_SWAP
    elif sys.platform.startswith("linux"):
        exchange = getattr(library, "renameat2", None)
        if exchange is None:
            raise OSError(errno.ENOTSUP, "atomic directory exchange is unavailable")
        exchange.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        exchange.restype = ctypes.c_int
        result = exchange(-100, os.fsencode(left), -100, os.fsencode(right), 2)
        # AT_FDCWD=-100; RENAME_EXCHANGE=2.
    else:
        raise OSError(errno.ENOTSUP, "atomic directory exchange is unavailable")
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(left))


def _publish_generation(
    payload: Path,
    mirror: Path,
    entries: list[Path],
    expected_digest: str,
    prior_docs: list[Path],
) -> None:
    """Install by building a complete generation, then switching the live mirror path atomically."""

    entries_set = set(entries)
    _validate_mirror_ownership(payload, mirror, entries, prior_docs)
    mirror.parent.mkdir(parents=True, exist_ok=True)

    staging = mirror.parent / f"{mirror.name}.next-{expected_digest[:12]}"
    retired = mirror.parent / f"{mirror.name}.retired-{uuid.uuid4().hex}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    generation_identity = None
    try:
        _copy_payload(payload, staging, entries)
        _merge_runtime_content(mirror, staging, entries_set, prior_docs, retired)
        installed_digest = snapshot_digest(staging)
        if installed_digest != expected_digest:
            raise RuntimeError(
                f"staged generation digest {installed_digest} != verified snapshot {expected_digest}"
            )
        # Put the complete generation at the eventual retained name first.
        # At exchange, the old live tree lands exactly where runtime symlinks
        # already point: no missing mirror or missing runtime-backing interval.
        staging.rename(retired)
        metadata = retired.stat()
        generation_identity = (metadata.st_dev, metadata.st_ino)
        if mirror.exists():
            _exchange_directories(mirror, retired)
        else:
            retired.rename(mirror)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if retired.exists():
            metadata = retired.stat()
            if (metadata.st_dev, metadata.st_ino) == generation_identity:
                # Publication failed before the swap; only private payload is removed.
                # After exchange the retained inode is the OLD live tree, including
                # writers' runtime backing. Preserve it even if the caller is interrupted
                # immediately after the syscall returns and before it records success.
                shutil.rmtree(retired)
        # Retained trees may still be held by active readers/writers. Reclamation
        # is deliberately outside the publisher.


def _write_runtime_registry(registry: Path, runtime_registry: Path) -> None:
    """Durably replace the separate registry using the validated deployment bytes.

    Failures before replace retain the old registry. Failures after replace leave
    the new bytes visible but propagate: durability is unconfirmed until retry.
    """

    runtime_registry.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{runtime_registry.name}.",
        suffix=".tmp",
        dir=runtime_registry.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.close(fd)
        shutil.copy2(registry, temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        temporary.replace(runtime_registry)
        directory_fd = os.open(runtime_registry.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def install(
    snapshot: Path,
    mirror: Path,
    expected_digest: str,
    runtime_registry: Path | None = None,
    *,
    verified: bool = True,
) -> int:
    snapshot = snapshot.resolve()
    mirror = mirror.expanduser().resolve()
    _validate_snapshot(snapshot)
    if re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
        raise ValueError("expected digest must be exactly 64 lowercase hexadecimal characters")
    if snapshot == mirror or snapshot in mirror.parents or mirror in snapshot.parents:
        raise ValueError("snapshot and mirror must be separate trees")
    if runtime_registry is not None:
        # Resolve the parent, not the leaf: the registry update replaces a leaf symlink
        # rather than following it. A parent alias into either tree still overlaps ownership.
        runtime_registry = runtime_registry.expanduser().absolute()
        runtime_registry = runtime_registry.parent.resolve() / runtime_registry.name
        if any(
            runtime_registry == root or root in runtime_registry.parents
            for root in (snapshot, mirror)
        ):
            raise ValueError("runtime registry must be outside the snapshot and mirror trees")

    entries = owned_entries(snapshot)
    actual_digest = snapshot_digest(snapshot)
    if actual_digest != expected_digest:
        raise ValueError(
            f"retained snapshot digest {actual_digest} != verified payload {expected_digest}"
        )
    _validate_payload(snapshot)
    prior_docs = _shipped_docs(mirror / ".docs-shipped.txt")

    with tempfile.TemporaryDirectory(prefix="orch-deployment-") as temporary_payload:
        payload = Path(temporary_payload)
        _copy_payload(snapshot, payload, entries)
        _validate_payload(payload)
        payload_digest = snapshot_digest(payload)
        if payload_digest != expected_digest:
            raise ValueError(
                f"staged payload digest {payload_digest} != verified payload {expected_digest}"
            )

        # Everything used below comes from the complete, validated private payload, including
        # the separate registry update. Never reopen the retained snapshot after this boundary.
        entries = owned_entries(payload)
        registry = payload / "repo_review_registry.json"
        mirror.parent.mkdir(parents=True, exist_ok=True)
        with (mirror.parent / f".{mirror.name}.publish.lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            # The previous publisher may have changed the shipped-doc manifest.
            prior_docs = _shipped_docs(mirror / ".docs-shipped.txt")
            _publish_generation(payload, mirror, entries, expected_digest, prior_docs)
            if runtime_registry is not None and registry.is_file():
                _write_runtime_registry(registry, runtime_registry)

    module_count = len(list(mirror.glob("*.py")))
    test_count = len(list((mirror / "tests").glob("*.py")))
    print(
        f"installed {'verified' if verified else 'UNVERIFIED'} snapshot {expected_digest[:12]}: "
        f"{module_count} modules, {test_count} test files -> {mirror}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("mirror", type=Path, nargs="?")
    parser.add_argument("--digest", action="store_true")
    parser.add_argument("--expected-digest")
    parser.add_argument("--runtime-registry", type=Path)
    parser.add_argument(
        "--unverified", action="store_true", help="digest is integrity only; no verifier verdict"
    )
    args = parser.parse_args(argv)
    try:
        if args.digest:
            if (
                args.mirror is not None
                or args.runtime_registry is not None
                or args.expected_digest is not None
                or args.unverified
            ):
                parser.error("--digest accepts only SNAPSHOT")
            print(snapshot_digest(args.snapshot))
            return 0
        if args.mirror is None:
            parser.error("MIRROR is required unless --digest is used")
        if args.expected_digest is None:
            parser.error("--expected-digest is required when installing")
        return install(
            args.snapshot,
            args.mirror,
            args.expected_digest,
            args.runtime_registry,
            verified=not args.unverified,
        )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"install-verified-snapshot: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
