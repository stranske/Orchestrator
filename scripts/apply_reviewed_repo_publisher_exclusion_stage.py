#!/usr/bin/env python3
"""Apply a reviewed publisher-exclusion stage under the controller writer lock.

This is deliberately inert until invoked with ``--apply``.  The staged recipe
is create-only for the new content-addressed helper and CAS-only for every
existing file.  It holds the publisher's existing ``.demand.lock`` throughout,
so a live controller writer causes a refusal rather than a replacement race.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any


class ApplyHeld(RuntimeError):
    pass


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def file_mode(path: Path) -> int:
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode) or path.is_symlink():
        raise ApplyHeld(f"regular non-symlink file required: {path}")
    return stat.S_IMODE(value.st_mode)


def _verify(path: Path, expected: dict[str, Any]) -> None:
    if not path.exists():
        raise ApplyHeld(f"canonical preimage missing: {path}")
    if digest(path) != expected["sha256"] or file_mode(path) != expected["mode"]:
        raise ApplyHeld(f"canonical preimage changed: {path}")


def _payload(stage: Path, name: str, expected_digest: str, expected_mode: int) -> bytes:
    path = stage / "payload" / name
    if path.name != name or not path.is_file() or path.is_symlink():
        raise ApplyHeld(f"invalid staged payload: {name}")
    if digest(path) != expected_digest or file_mode(path) != expected_mode:
        raise ApplyHeld(f"staged payload changed: {name}")
    return path.read_bytes()


def _fsync_parent(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _create_only(path: Path, raw: bytes, expected_mode: int) -> None:
    if path.exists() or path.is_symlink():
        raise ApplyHeld(f"content-addressed helper already exists: {path}")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, expected_mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        raise
    os.chmod(path, expected_mode)
    _fsync_parent(path)


def _replace_cas(path: Path, raw: bytes, expected: dict[str, Any], expected_mode: int) -> None:
    _verify(path, expected)
    descriptor, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, expected_mode)
        os.replace(temporary, path)
        _fsync_parent(path)
    finally:
        temporary.unlink(missing_ok=True)


def apply(stage: Path) -> dict[str, Any]:
    recipe = json.loads((stage / "apply-recipe.json").read_text())
    if recipe.get("schema") != "reviewed-repo-publisher-exclusion-stage/v1":
        raise ApplyHeld("unsupported stage recipe")
    root = Path(recipe["controller_root"]).resolve()
    if root / ".demand.lock" != Path(recipe["lock"]):
        raise ApplyHeld("recipe lock is not controller-local")
    lock = root / ".demand.lock"
    with lock.open("a+b") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ApplyHeld("active controller writer holds .demand.lock") from exc
        preimages = recipe.get("preimages") or {}
        for name, expected in preimages.items():
            _verify(Path(expected["path"]), expected)
        applied = []
        for operation in recipe.get("operations") or []:
            target = root / operation["target"]
            if target.name != operation["target"]:
                raise ApplyHeld("nested or absolute apply target refused")
            raw = _payload(stage, operation["payload"], operation["sha256"], operation["mode"])
            if operation["kind"] == "create_only":
                _create_only(target, raw, operation["mode"])
            elif operation["kind"] == "replace_cas":
                _replace_cas(target, raw, preimages[operation["target"]], operation["mode"])
            else:
                raise ApplyHeld("unknown apply operation")
            if digest(target) != operation["sha256"] or file_mode(target) != operation["mode"]:
                raise ApplyHeld(f"post-write verification failed: {target}")
            applied.append(operation["target"])
    return {"applied": applied, "controller_root": str(root)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stage", required=True)
    parser.add_argument(
        "--apply", action="store_true", help="required; otherwise only validates arguments"
    )
    args = parser.parse_args(argv)
    if not args.apply:
        parser.error("--apply is required; this command never applies implicitly")
    print(json.dumps(apply(Path(args.stage).resolve()), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
