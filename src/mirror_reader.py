"""Run a mirror reader while excluding publication, including its shell children.

The lock lives beside the mirror so directory exchange never changes its inode.
The descriptor survives exec into the tick shell and its shell children. This is
reader exclusion, not a claim that all entry points pin.
"""

from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path

LOCK_ENV = "ORCH_PUBLICATION_READER_FD"


def lock_path(root: Path) -> Path:
    root = root.expanduser().resolve()
    return root.parent / f".{root.name}.publish.lock"


def inherited_lock(root: Path) -> bool:
    """Validate the inherited descriptor against this mirror, then hold shared mode."""
    try:
        fd = int(os.environ[LOCK_ENV])
        held = os.fstat(fd)
        expected = lock_path(root).stat()
        if (held.st_dev, held.st_ino) != (expected.st_dev, expected.st_ino):
            return False
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return True
    except (KeyError, ValueError, OSError):
        return False


def run(root: Path, command: list[str]) -> int:
    """Acquire before reopening executable code; retain exclusion through command exit."""
    with lock_path(root).open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
        env = dict(os.environ, **{LOCK_ENV: str(lock.fileno())})
        os.set_inheritable(lock.fileno(), True)
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
