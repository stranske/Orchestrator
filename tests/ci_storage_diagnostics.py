"""Non-authoritative, metadata-only measurements of this pytest invocation's storage."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path

MAX_ENTRIES = 100_000
MAX_SECONDS = 3.0


def snapshot(root: Path, *, scan: bool = True) -> dict:
    """Never follow symlinks, read file contents, remove files, or change quota."""
    result: dict = {"schema": "pytest-owned-storage/v1"}
    root_fd = None
    try:
        root = Path(root)
        if not root.is_absolute() or root.resolve(strict=True) != root or not root.is_dir():
            return {**result, "status": "unavailable", "reason": "nonphysical_base"}
        expected = root.lstat()
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        opened = os.fstat(root_fd)
        if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
            return {**result, "status": "unavailable", "reason": "base_identity_changed"}
        fs = os.fstatvfs(root_fd)
        result["filesystem"] = {
            "free_bytes": fs.f_bavail * fs.f_frsize,
            "free_inodes": fs.f_favail,
        }
        quota = shutil.which("quota")
        result["quota"] = {"status": "unsupported"}
        if quota and sys_platform_linux():
            try:
                # Local filesystems only; no remote quota server or modifying command.
                proc = subprocess.run(
                    [quota, "-l", "-w", "-p", "-u", str(os.getuid())],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )
                # Never print account names, mount names, raw stdout, or diagnostics.
                result["quota"] = {
                    "status": "observed" if proc.returncode == 0 else "unavailable",
                    "exit": proc.returncode,
                    "numeric_rows": [
                        [int(v) for v in re.findall(r"(?<!\S)\d+(?!\S)", row)]
                        for row in proc.stdout[:8192].splitlines()
                        if re.findall(r"(?<!\S)\d+(?!\S)", row)
                    ][:16],
                    "output_truncated": len(proc.stdout) > 8192,
                }
            except (OSError, subprocess.SubprocessError):
                result["quota"] = {"status": "unavailable"}
        if not scan:
            return {**result, "status": "observed", "tree": {"status": "not_scanned"}}
        result["tree"] = _owned_tree(root_fd)
        return {**result, "status": "observed"}
    except Exception as exc:
        # A diagnostic must never replace the original test/worker outcome.
        return {**result, "status": "unavailable", "error_type": type(exc).__name__}

    finally:
        if root_fd is not None:
            os.close(root_fd)


def _owned_tree(root_fd: int) -> dict:
    """Descend only via no-follow descriptors joined to the enumerated inode."""
    deadline = time.monotonic() + MAX_SECONDS
    entries = inaccessible = allocated = logical = 0
    totals: dict[str, int] = {}
    stack = []
    complete = True
    initial = os.dup(root_fd)
    try:
        try:
            stack.append((initial, os.scandir(initial), "."))
        except OSError:
            os.close(initial)
            inaccessible += 1
        while stack:
            if entries >= MAX_ENTRIES or time.monotonic() >= deadline:
                complete = False
                break
            directory_fd, iterator, bucket = stack[-1]
            try:
                entry = next(iterator)
            except (StopIteration, OSError) as exc:
                if isinstance(exc, OSError):
                    inaccessible += 1
                iterator.close()
                os.close(directory_fd)
                stack.pop()
                continue
            entries += 1
            child_fd = None
            try:
                info = entry.stat(follow_symlinks=False)
                key = entry.name if len(stack) == 1 else bucket
                size = getattr(info, "st_blocks", 0) * 512
                allocated += size
                totals[key] = totals.get(key, 0) + size
                if stat.S_ISREG(info.st_mode):
                    logical += info.st_size
                elif stat.S_ISDIR(info.st_mode):
                    child_fd = os.open(
                        entry.name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=directory_fd,
                    )
                    opened = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        inaccessible += 1
                        continue
                    stack.append((child_fd, os.scandir(child_fd), key))
                    child_fd = None  # The stack now owns this descriptor.
            except OSError:
                inaccessible += 1
            finally:
                if child_fd is not None:
                    os.close(child_fd)
    finally:
        for directory_fd, iterator, _ in reversed(stack):
            iterator.close()
            os.close(directory_fd)
    return {
        "status": "complete" if complete and not inaccessible else "partial",
        "entries_seen": entries,
        "inaccessible": inaccessible,
        "bounded": not complete,
        "allocated_bytes_seen": allocated,
        "logical_file_bytes_seen": logical,
        "largest_owned_children": sorted(totals.items(), key=lambda row: (-row[1], row[0]))[:10],
    }


def sys_platform_linux() -> bool:
    import sys

    return sys.platform.startswith("linux")
