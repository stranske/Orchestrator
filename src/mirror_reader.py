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


def run(root: Path, command: list[str]) -> int:
    """Select under the publication lock, then reopen code through its retained path."""
    root = mirror_path(root)
    with lock_path(root).open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
        pinned = root.resolve(strict=True)
        modules = pinned / "src" if (pinned / "src").is_dir() else pinned

        def pin_argument(argument: str) -> str:
            # Rewrite only path arguments inside the mirror, without interpreting
            # shell strings or changing unrelated arguments containing its name.
            path = Path(argument)
            if path.is_absolute() and path.is_relative_to(root):
                return str(pinned / path.relative_to(root))
            return argument

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
                        *filter(None, os.environ.get("PYTHONPATH", "").split(os.pathsep)),
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
        # Keep the original tick PID: its already-armed watchdog also observes a
        # blocked lock acquisition. Bash retains this fd while waiting for Python.
        os.execvpe(command[0], command, env)
        raise AssertionError("exec returned")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "run"))
    parser.add_argument("root", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.mode == "check":
        return 0 if inherited_lock(args.root) else 1
    if not args.command:
        parser.error("run requires a command")
    return run(args.root, args.command)


if __name__ == "__main__":
    raise SystemExit(main())
