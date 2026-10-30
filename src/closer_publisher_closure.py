"""Strict immutable capture and byte-backed loading for the standing publisher."""

from __future__ import annotations

import ast
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, cast

MODULES = (
    "controller.py",
    "demand_router.py",
    "accountability.py",
    "maintenance_admission.py",
    "repo_campaigns.py",
    "followthrough_control.py",
    "followthrough_observer.py",
    "maintenance_receiver.py",
)
DYNAMIC_HELPER = (
    "delivery-continuity-a1720a1d8e922529db556ecd989792d5410e85d6ef627e385584f20b9b1cd40e.py"
)
_HEX = set("0123456789abcdef")


class PublisherClosureError(ValueError):
    pass


def _canonical(value: Any, what: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or path.resolve() != path or not path.is_dir():
        raise PublisherClosureError(f"{what} is not canonical")
    return path


def _file(root: Path, name: str) -> Path:
    if not isinstance(name, str) or Path(name).name != name or not name.endswith(".py"):
        raise PublisherClosureError("publisher closure path is not a flat Python filename")
    path = root / name
    if path.is_symlink() or path.resolve() != path or path.parent != root or not path.is_file():
        raise PublisherClosureError("publisher closure path escapes or is not a regular file")
    return path


def _pin(path: Path) -> dict[str, Any]:
    return {
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "mode": stat.S_IMODE(path.stat().st_mode),
    }


def _pin_ok(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"sha256", "mode"}
        and isinstance(value["sha256"], str)
        and len(value["sha256"]) == 64
        and not (set(value["sha256"]) - _HEX)
        and isinstance(value["mode"], int)
        and not value["mode"] & ~0o777
    )


def _check_imports(modules: dict[str, Any], root: Path) -> None:
    local = {Path(name).stem for name in modules}
    for name in modules:
        try:
            tree = ast.parse((root / name).read_bytes(), filename=name)
        except SyntaxError as exc:
            raise PublisherClosureError(f"publisher source does not parse: {name}") from exc
        for node in ast.walk(tree):
            imports: tuple[str, ...] = ()
            if isinstance(node, ast.Import):
                imports = tuple(item.name for item in node.names)
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports = (node.module,)
            for item in imports:
                top = item.partition(".")[0]
                if (
                    top not in local
                    and top not in sys.stdlib_module_names
                    and top not in sys.builtin_module_names
                ):
                    raise PublisherClosureError(f"publisher has unlisted non-stdlib import: {top}")


def validate(value: Any) -> dict[str, Any]:
    keys = {
        "schema",
        "root",
        "controller_data_root",
        "entry_module",
        "entry_function",
        "modules",
        "dynamic_helpers",
        "capture",
    }
    if (
        not isinstance(value, dict)
        or set(value) != keys
        or value.get("schema") != "closer-publisher-closure/v2"
    ):
        raise PublisherClosureError("unsupported publisher closure descriptor")
    root = _canonical(value["root"], "publisher closure root")
    data_root = _canonical(value["controller_data_root"], "publisher mutable data root")
    modules, helpers, capture = value["modules"], value["dynamic_helpers"], value["capture"]
    if (
        not isinstance(modules, dict)
        or set(modules) != set(MODULES)
        or not isinstance(helpers, dict)
        or set(helpers) != {DYNAMIC_HELPER}
    ):
        raise PublisherClosureError("publisher closure files differ from the reviewed closure")
    if (
        not isinstance(capture, dict)
        or set(capture) != {"source_root", "source_snapshot_receipt", "source_capture_tool"}
        or not isinstance(capture["source_root"], str)
    ):
        raise PublisherClosureError("publisher capture provenance is invalid")
    for key in ("source_snapshot_receipt", "source_capture_tool"):
        item = capture[key]
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "sha256"}
            or not isinstance(item["path"], str)
            or not isinstance(item["sha256"], str)
            or len(item["sha256"]) != 64
            or set(item["sha256"]) - _HEX
        ):
            raise PublisherClosureError("publisher capture provenance is invalid")
    for name, pin in {**modules, **helpers}.items():
        if not _pin_ok(pin) or _pin(_file(root, name)) != pin:
            raise PublisherClosureError("publisher closure bytes or mode changed")
    if value["entry_module"] != "demand_router" or value["entry_function"] != "main":
        raise PublisherClosureError("publisher entrypoint differs from the reviewed router")
    _check_imports(modules, root)
    return {
        **value,
        "root": str(root),
        "controller_data_root": str(data_root),
        "modules": dict(modules),
        "dynamic_helpers": dict(helpers),
        "capture": dict(capture),
    }


def capture(
    source_root: Path,
    out: Path,
    *,
    controller_data_root: Path,
    source_snapshot_receipt: Path,
    source_capture_tool: Path,
) -> dict[str, Any]:
    source_root, controller_data_root = _canonical(
        source_root, "publisher source root"
    ), _canonical(controller_data_root, "publisher mutable data root")
    source_snapshot_receipt, source_capture_tool = Path(source_snapshot_receipt), Path(
        source_capture_tool
    )
    if (
        out.exists()
        or out.parent.is_symlink()
        or not all(
            (source_root / x).is_file() and not (source_root / x).is_symlink()
            for x in (*MODULES, DYNAMIC_HELPER)
        )
    ):
        raise PublisherClosureError("capture source or output is unsafe")
    if any(
        not path.is_file() or path.is_symlink() or path.resolve() != path
        for path in (source_snapshot_receipt, source_capture_tool)
    ):
        raise PublisherClosureError("capture provenance file is unsafe")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out.parent, prefix=".publisher-capture-") as temp:
        staged, pins = Path(temp), {}
        for name in (*MODULES, DYNAMIC_HELPER):
            source, target = source_root / name, Path(temp) / name
            target.write_bytes(source.read_bytes())
            os.chmod(target, stat.S_IMODE(source.stat().st_mode))
            pins[name] = _pin(target)
        _check_imports({name: pins[name] for name in MODULES}, staged)
        descriptor = {
            "schema": "closer-publisher-closure/v2",
            "root": str(out.resolve()),
            "controller_data_root": str(controller_data_root),
            "entry_module": "demand_router",
            "entry_function": "main",
            "modules": {x: pins[x] for x in MODULES},
            "dynamic_helpers": {DYNAMIC_HELPER: pins[DYNAMIC_HELPER]},
            "capture": {
                "source_root": str(source_root),
                "source_snapshot_receipt": {
                    "path": str(source_snapshot_receipt),
                    "sha256": hashlib.sha256(source_snapshot_receipt.read_bytes()).hexdigest(),
                },
                "source_capture_tool": {
                    "path": str(source_capture_tool),
                    "sha256": hashlib.sha256(source_capture_tool.read_bytes()).hexdigest(),
                },
            },
        }
        (staged / "publisher-closure.json").write_text(
            json.dumps(descriptor, sort_keys=True, separators=(",", ":")) + "\n"
        )
        os.replace(staged, out)
    return validate(descriptor)


class _Loader(importlib.abc.Loader):
    def __init__(self, name, source, origin, data_origin):
        self.name, self.source, self.origin, self.data_origin = name, source, origin, data_origin

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        module.__file__, module.__loader__ = str(self.data_origin), self
        exec(compile(self.source, str(self.origin), "exec", dont_inherit=True), module.__dict__)


class _Finder(importlib.abc.MetaPathFinder):
    def __init__(self, sources):
        self.sources = sources

    def find_spec(self, fullname, path=None, target=None):
        item = self.sources.get(fullname)
        if item:
            return importlib.machinery.ModuleSpec(
                fullname, _Loader(fullname, *item), origin=str(item[1])
            )
        if fullname.partition(".")[0] not in sys.stdlib_module_names | sys.builtin_module_names:
            raise PublisherClosureError("publisher attempted an unlisted non-stdlib import")
        return None


def load_entrypoint(
    value: Any,
) -> tuple[Callable[[list[str] | None], int], Callable[[], dict[str, Any]]]:
    descriptor = validate(value)
    root, data_root = Path(descriptor["root"]), Path(descriptor["controller_data_root"])
    names = {Path(x).stem for x in (*descriptor["modules"], *descriptor["dynamic_helpers"])}
    if names & set(sys.modules):
        raise PublisherClosureError("publisher closure module is already cached")
    sources = {
        Path(x).stem: ((root / x).read_bytes(), root / x, data_root / x)
        for x in (*descriptor["modules"], *descriptor["dynamic_helpers"])
    }
    finder, original = _Finder(sources), importlib.util.spec_from_file_location
    helper = data_root / DYNAMIC_HELPER

    def guarded(name, location, *args, **kwargs):
        if Path(location) == helper and name == "_pinned_delivery_continuity":
            return importlib.machinery.ModuleSpec(
                name,
                _Loader(name, *sources[Path(DYNAMIC_HELPER).stem]),
                origin=str(root / DYNAMIC_HELPER),
            )
        raise PublisherClosureError("publisher attempted an unlisted dynamic import")

    @contextmanager
    def isolated():
        old_path, old_bytecode = list(sys.path), sys.dont_write_bytecode
        sys.path[:] = [x for x in sys.path if Path(x or ".").resolve() not in {root, data_root}]
        sys.meta_path.insert(0, finder)
        sys.dont_write_bytecode = True
        importlib.util.spec_from_file_location = guarded
        try:
            yield
        finally:
            importlib.util.spec_from_file_location = original
            sys.meta_path.remove(finder)
            sys.path[:] = old_path
            sys.dont_write_bytecode = old_bytecode

    try:
        with isolated():
            module = importlib.import_module(descriptor["entry_module"])
            entry = getattr(module, descriptor["entry_function"], None)
            if not callable(entry):
                raise PublisherClosureError("publisher entrypoint is not callable")
            typed_entry = cast(Callable[[list[str] | None], int], entry)

            def run(argv: list[str] | None) -> int:
                with isolated():
                    return typed_entry(argv)

        def after() -> dict[str, Any]:
            return validate(descriptor)

        return run, after
    except Exception:
        for name in names | {"_pinned_delivery_continuity"}:
            sys.modules.pop(name, None)
        raise
