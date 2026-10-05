"""Pin executable paths for a reader and its children.

The lock lives beside the mirror so directory exchange never changes its inode.
The descriptor survives exec into the tick shell and its shell children. Shared
exclusion protects initial real-directory migration; permanently named generations
need the lock only while selecting their path because publication retains them.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import sys
import time
from pathlib import Path

LOCK_ENV = "ORCH_PUBLICATION_READER_FD"
ROOT_ENV = "ORCH_PUBLICATION_ROOT"
GENERATION_ENV = "ORCH_PUBLICATION_GENERATION"


def mirror_path(root: Path) -> Path:
    """Canonicalize the parent, preserving the reader-visible publication link."""
    root = root.expanduser().absolute()
    return root.parent.resolve() / root.name


def lock_path(root: Path) -> Path:
    root = mirror_path(root)
    return root.parent / f".{root.name}.publish.lock"


def retained_generation(root: Path, pinned: Path) -> bool:
    return pinned.parent == root.parent and pinned.name.startswith(root.name + ".generation-")


def inherited_lock(root: Path) -> bool:
    """Validate a retained pin or hold shared mode for the incumbent real directory."""
    try:
        fd = int(os.environ[LOCK_ENV])
        held = os.fstat(fd)
        # The reentered tick runs from a physical generation, while the lock is
        # permanently anchored to the logical mirror. Do not consult the live
        # link again to select executable paths after publication.
        logical = mirror_path(Path(os.environ.get(ROOT_ENV, str(root))))
        pinned = Path(os.environ.get(GENERATION_ENV, str(logical))).resolve()
        if root.resolve() != pinned:
            return False
        expected = lock_path(logical).stat()
        if (held.st_dev, held.st_ino) != (expected.st_dev, expected.st_ino):
            return False
        if not retained_generation(logical, pinned):
            if pinned != logical.resolve():
                return False
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return True
    except (KeyError, ValueError, OSError):
        return False


def run(root: Path, command: list[str], lock_timeout: float = 30.0) -> int:
    """Select under the publication lock, then reopen code through its retained path."""
    root = mirror_path(root)
    with lock_path(root).open("a") as lock:
        deadline = time.monotonic() + lock_timeout
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"publication reader lock deadline exceeded: {root}")
                time.sleep(min(0.05, remaining))
        pinned = root.resolve(strict=True)
        modules = pinned / "src" if (pinned / "src").is_dir() else pinned

        def pin_argument(argument: str) -> str:
            # Rewrite only path arguments inside the mirror, without interpreting
            # shell strings or changing unrelated arguments containing its name.
            path = Path(argument)
            if path.is_absolute():
                # Normalize dot segments without resolving the live publication link.
                path = Path(os.path.abspath(path))
            if path.is_absolute() and path.is_relative_to(root):
                return str(pinned / path.relative_to(root))
            return argument

        def pin_import_path(entry: str) -> str:
            # PYTHONPATH entries may be relative to the launch directory. Use
            # that directory before exec (or any cwd change) to identify mirror
            # aliases, without resolving the movable publication link again.
            # Preserve unrelated entries, including their relative spelling.
            absolute = Path(os.path.abspath(entry))
            if absolute.is_relative_to(root):
                return str(pinned / absolute.relative_to(root))
            return entry

        command = [pin_argument(argument) for argument in command]
        env = dict(
            os.environ,
            **{
                LOCK_ENV: str(lock.fileno()),
                ROOT_ENV: str(root),
                GENERATION_ENV: str(pinned),
                "ORCH_DIR": str(pinned),
                "PYTHONPATH": os.pathsep.join(
                    [
                        str(modules),
                        *(
                            pin_import_path(entry)
                            for entry in filter(
                                None, os.environ.get("PYTHONPATH", "").split(os.pathsep)
                            )
                        ),
                    ]
                ),
            },
        )
        # A relative import in a child must use the same physical directory too.
        cwd = Path.cwd()
        if cwd.is_relative_to(root):
            os.chdir(pinned / cwd.relative_to(root))
        os.set_inheritable(lock.fileno(), True)
        if retained_generation(root, pinned):
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        # Keep the original tick PID for the watchdog armed after reentry. Bash
        # retains this fd while waiting for Python during initial migration.
        os.execvpe(command[0], command, env)
        raise AssertionError("exec returned")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock-timeout", type=float, default=30.0)
    parser.add_argument("mode", choices=("check", "run"))
    parser.add_argument("root", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not 0 < args.lock_timeout <= 300:
        parser.error("lock timeout must be positive and at most 300 seconds")
    if args.mode == "check":
        return 0 if inherited_lock(args.root) else 1
    if not args.command:
        parser.error("run requires a command")
    try:
        return run(args.root, args.command, args.lock_timeout)
    except TimeoutError as error:
        print(str(error), file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
