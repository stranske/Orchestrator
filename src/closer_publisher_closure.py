"""Strict immutable capture and byte-backed loading for the standing publisher."""

from __future__ import annotations

import ast
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import re
import stat
import sys
import sysconfig
import tempfile
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping, cast

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
    "delivery-continuity-5202c248a71d5b45a324d7e98b1865a43f1b20fe56b6903330ae153a314394e2.py"
)
EXTERNAL_BINDINGS = (
    ("accountability_guard", "/Users/teacher/.codex/bin/opener-liveness-guard.py"),
    (
        "maintenance_campaign_budget",
        "/Users/teacher/.codex/automations/sync-dependency-pr-closer/campaign_budget.py",
    ),
    ("maintenance_owner_lock", "/Users/teacher/.codex/bin/sync-dependency-lock.py"),
)
CAPTURE_IMPLEMENTATION = Path(__file__).name
SOURCE_RECEIPT_KEYS = frozenset(
    {
        "receipt_path",
        "receipt_sha256",
        "candidate_sha",
        "snapshot_path",
        "snapshot_digest",
        "source_identity",
    }
)
SOURCE_CAPTURE_TOOL_KEYS = frozenset(
    {"capture_tool", "capture_tool_sha256", "implementation_sha256"}
)
PACKAGE_DESCRIPTOR = "publisher-closure.json"
INCOMPLETE_MARKER = ".publisher-incomplete"
_HEX = set("0123456789abcdef")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")


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


def _digest_ok(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and not (set(value) - _HEX)


def _implementation_path() -> Path:
    path = Path(__file__).resolve()
    if path.is_symlink() or not path.is_file():
        raise PublisherClosureError("publisher capture implementation is unavailable")
    return path


@lru_cache(maxsize=1)
def _snapshot_digest_fn():
    from source_snapshot_digest import snapshot_digest

    return snapshot_digest


def _fsync_path(path: Path) -> None:
    if path.is_dir():
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _write_durable_bytes(path: Path, payload: bytes, mode: int) -> None:
    path.write_bytes(payload)
    os.chmod(path, mode)
    _fsync_path(path)


def _validate_release_record(record: dict[str, Any]) -> dict[str, Any]:
    import closer_release

    closer_release._validate_record(record)
    verification = record.get("verification")
    if not isinstance(verification, dict) or not verification.get("verified"):
        raise PublisherClosureError("Source release receipt is not verified")
    for key in ("snapshot_path", "snapshot_digest", "source_identity"):
        if not verification.get(key):
            raise PublisherClosureError("Source release receipt is not verified")
    if not _digest_ok(verification["snapshot_digest"]):
        raise PublisherClosureError("Source release receipt is not verified")
    if not _SHA40.fullmatch(str(verification["source_identity"])):
        raise PublisherClosureError("Source release receipt is not verified")
    if not _SHA40.fullmatch(str(record.get("candidate_sha", ""))):
        raise PublisherClosureError("Source release receipt is not verified")
    return record


def _provenance_shape(capture: Mapping[str, Any]) -> None:
    if (
        not isinstance(capture, dict)
        or set(capture) != {"source_root", "source_snapshot_receipt", "source_capture_tool"}
        or not isinstance(capture["source_root"], str)
    ):
        raise PublisherClosureError("publisher capture provenance is invalid")
    receipt, tool = capture["source_snapshot_receipt"], capture["source_capture_tool"]
    if not isinstance(receipt, dict) or set(receipt) != SOURCE_RECEIPT_KEYS:
        raise PublisherClosureError("publisher capture provenance is invalid")
    if not isinstance(tool, dict) or set(tool) != SOURCE_CAPTURE_TOOL_KEYS:
        raise PublisherClosureError("publisher capture provenance is invalid")
    if not _digest_ok(receipt["receipt_sha256"]) or not _digest_ok(receipt["snapshot_digest"]):
        raise PublisherClosureError("publisher capture provenance is invalid")
    if not _digest_ok(tool["capture_tool_sha256"]) or not _digest_ok(tool["implementation_sha256"]):
        raise PublisherClosureError("publisher capture provenance is invalid")
    if not _SHA40.fullmatch(str(receipt.get("candidate_sha", ""))) or not _SHA40.fullmatch(
        str(receipt.get("source_identity", ""))
    ):
        raise PublisherClosureError("publisher capture provenance is invalid")
    if tool.get("capture_tool") != CAPTURE_IMPLEMENTATION:
        raise PublisherClosureError("publisher capture provenance is invalid")


def _verify_qualified_source(capture: Mapping[str, Any]) -> None:
    _provenance_shape(capture)
    receipt, tool = capture["source_snapshot_receipt"], capture["source_capture_tool"]
    receipt_path = Path(receipt["receipt_path"])
    if (
        not receipt_path.is_file()
        or receipt_path.is_symlink()
        or hashlib.sha256(receipt_path.read_bytes()).hexdigest() != receipt["receipt_sha256"]
    ):
        raise PublisherClosureError("Source release receipt is invalid")
    try:
        record = _validate_release_record(json.loads(receipt_path.read_text()))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise PublisherClosureError("Source release receipt is invalid") from exc
    verification = record["verification"]
    if (
        record["candidate_sha"] != receipt["candidate_sha"]
        or verification["snapshot_path"] != receipt["snapshot_path"]
        or verification["snapshot_digest"] != receipt["snapshot_digest"]
        or verification["source_identity"] != receipt["source_identity"]
    ):
        raise PublisherClosureError("Source release receipt is not verified")
    snapshot = Path(receipt["snapshot_path"])
    if not snapshot.is_dir() or snapshot.is_symlink():
        raise PublisherClosureError("Source release receipt is not verified")
    digest_fn = _snapshot_digest_fn()
    try:
        if digest_fn(snapshot) != receipt["snapshot_digest"]:
            raise PublisherClosureError("Source release snapshot digest differs")
    except ValueError as exc:
        raise PublisherClosureError("Source release snapshot digest differs") from exc
    implementation = _implementation_path()
    if (
        tool["implementation_sha256"] != hashlib.sha256(implementation.read_bytes()).hexdigest()
        or tool["capture_tool_sha256"] != tool["implementation_sha256"]
    ):
        raise PublisherClosureError("capture implementation differs from the standing loader")
    member = snapshot / CAPTURE_IMPLEMENTATION
    if (
        not member.is_file()
        or member.is_symlink()
        or hashlib.sha256(member.read_bytes()).hexdigest() != tool["capture_tool_sha256"]
    ):
        raise PublisherClosureError(
            "capture implementation is not a member of the verified Source snapshot"
        )


def _bind_source_provenance(
    receipt_path: Path, capture_tool: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    implementation = _implementation_path()
    if capture_tool.resolve() != implementation:
        raise PublisherClosureError("capture tool must be the standing publisher implementation")
    try:
        record = _validate_release_record(json.loads(receipt_path.read_text()))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise PublisherClosureError("Source release receipt is invalid") from exc
    verification = record["verification"]
    snapshot = Path(verification["snapshot_path"])
    if not snapshot.is_dir() or snapshot.is_symlink():
        raise PublisherClosureError("Source release receipt is not verified")
    digest_fn = _snapshot_digest_fn()
    try:
        if digest_fn(snapshot) != verification["snapshot_digest"]:
            raise PublisherClosureError("Source release snapshot digest differs")
    except ValueError as exc:
        raise PublisherClosureError("Source release snapshot digest differs") from exc
    tool_hash = hashlib.sha256(implementation.read_bytes()).hexdigest()
    member = snapshot / CAPTURE_IMPLEMENTATION
    if (
        not member.is_file()
        or member.is_symlink()
        or hashlib.sha256(member.read_bytes()).hexdigest() != tool_hash
    ):
        raise PublisherClosureError(
            "capture implementation is not a member of the verified Source snapshot"
        )
    receipt_binding = {
        "receipt_path": str(receipt_path.resolve()),
        "receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        "candidate_sha": record["candidate_sha"],
        "snapshot_path": str(snapshot.resolve()),
        "snapshot_digest": verification["snapshot_digest"],
        "source_identity": verification["source_identity"],
    }
    tool_binding = {
        "capture_tool": CAPTURE_IMPLEMENTATION,
        "capture_tool_sha256": tool_hash,
        "implementation_sha256": tool_hash,
    }
    return receipt_binding, tool_binding


def _check_imports(
    modules: dict[str, Any], root: Path, *, extra_stems: frozenset[str] = frozenset()
) -> None:
    local = {Path(name).stem for name in modules} | set(extra_stems)
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
        "external_bindings",
    }
    if (
        not isinstance(value, dict)
        or set(value) != keys
        or value.get("schema") != "closer-publisher-closure/v2"
    ):
        raise PublisherClosureError("unsupported publisher closure descriptor")
    root = _canonical(value["root"], "publisher closure root")
    if (root / INCOMPLETE_MARKER).exists() or (root / INCOMPLETE_MARKER).is_symlink():
        raise PublisherClosureError("publisher package publish is incomplete")
    data_root = _canonical(value["controller_data_root"], "publisher mutable data root")
    if root == data_root or root in data_root.parents or data_root in root.parents:
        raise PublisherClosureError("publisher code and mutable data roots overlap")
    modules, helpers, capture, externals = (
        value["modules"],
        value["dynamic_helpers"],
        value["capture"],
        value["external_bindings"],
    )
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
    _provenance_shape(capture)
    _verify_qualified_source(capture)
    for name, pin in {**modules, **helpers}.items():
        if not _pin_ok(pin) or _pin(_file(root, name)) != pin:
            raise PublisherClosureError("publisher closure bytes or mode changed")
    if not isinstance(externals, list) or len(externals) != len(EXTERNAL_BINDINGS):
        raise PublisherClosureError("publisher external binding set differs")
    expected_external = {name: path for name, path in EXTERNAL_BINDINGS}
    if {item.get("module_name") for item in externals if isinstance(item, dict)} != set(
        expected_external
    ):
        raise PublisherClosureError("publisher external binding names differ")
    for item in externals:
        if (
            not isinstance(item, dict)
            or set(item) != {"module_name", "source_path", "package_file", "pin"}
            or item.get("module_name") not in expected_external
            or item.get("source_path") != expected_external[item["module_name"]]
            or not _pin_ok(item.get("pin"))
            or _pin(_file(root, item["package_file"])) != item["pin"]
        ):
            raise PublisherClosureError("publisher external binding is invalid")
    if value["entry_module"] != "demand_router" or value["entry_function"] != "main":
        raise PublisherClosureError("publisher entrypoint differs from the reviewed router")
    external_stems = frozenset(str(item["module_name"]) for item in externals)
    _check_imports({**modules, **helpers}, root, extra_stems=external_stems)
    _check_imports(
        {item["package_file"]: item["pin"] for item in externals},
        root,
        extra_stems=external_stems,
    )
    return {
        **value,
        "root": str(root),
        "controller_data_root": str(data_root),
        "modules": dict(modules),
        "dynamic_helpers": dict(helpers),
        "capture": dict(capture),
        "external_bindings": list(externals),
    }


def validate_controller_demand_contract(value: Any) -> dict[str, Any]:
    """Validate v2 demand fields binding mutable state to captured code."""
    required = {
        "schema",
        "controller_root",
        "controller_data_root",
        "campaign_state",
        "campaign_id",
        "owner",
        "publisher_sha256",
        "publisher_closure",
    }
    if (
        not isinstance(value, dict)
        or value.get("schema") != "closer-controller-demand/v2"
        or not required <= value.keys()
        or set(value) - required - {"worker_config"}
    ):
        raise PublisherClosureError("unsupported v2 controller demand contract")
    data_root = _canonical(value["controller_data_root"], "publisher mutable data root")
    controller_root = _canonical(value["controller_root"], "publisher controller root")
    if data_root != controller_root:
        raise PublisherClosureError("publisher mutable data root differs from controller root")
    if any(
        not isinstance(value.get(key), str) or not value[key]
        for key in ("campaign_state", "campaign_id", "owner")
    ):
        raise PublisherClosureError("v2 controller demand identity is invalid")
    closure = validate(value["publisher_closure"])
    if Path(closure["controller_data_root"]) != data_root:
        raise PublisherClosureError("publisher closure data root differs from controller contract")
    if (
        not isinstance(value.get("publisher_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", value["publisher_sha256"])
        or value["publisher_sha256"] != closure["modules"]["demand_router.py"]["sha256"]
    ):
        raise PublisherClosureError("publisher closure router pin differs from controller contract")
    return closure


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
    source_provenance = _bind_source_provenance(source_snapshot_receipt, source_capture_tool)
    external_paths = [(name, Path(path)) for name, path in EXTERNAL_BINDINGS]
    if any(not path.is_file() or path.is_symlink() for _name, path in external_paths):
        raise PublisherClosureError("publisher external dependency is unavailable")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out.parent, prefix=".publisher-capture-") as temp:
        staged, pins = Path(temp), {}
        for name in (*MODULES, DYNAMIC_HELPER):
            source, target = source_root / name, staged / name
            _write_durable_bytes(target, source.read_bytes(), stat.S_IMODE(source.stat().st_mode))
            pins[name] = _pin(target)
        external = []
        for module_name, source in external_paths:
            package_file = "external-" + module_name + ".py"
            target = staged / package_file
            _write_durable_bytes(target, source.read_bytes(), stat.S_IMODE(source.stat().st_mode))
            external.append(
                {
                    "module_name": module_name,
                    "source_path": str(source),
                    "package_file": package_file,
                    "pin": _pin(target),
                }
            )
        external_stems = frozenset(str(item["module_name"]) for item in external)
        _check_imports(pins, staged, extra_stems=external_stems)
        descriptor = {
            "schema": "closer-publisher-closure/v2",
            "root": str(out.resolve()),
            "controller_data_root": str(controller_data_root),
            "entry_module": "demand_router",
            "entry_function": "main",
            "modules": {x: pins[x] for x in MODULES},
            "dynamic_helpers": {DYNAMIC_HELPER: pins[DYNAMIC_HELPER]},
            "external_bindings": external,
            "capture": {
                "source_root": str(source_root),
                "source_snapshot_receipt": source_provenance[0],
                "source_capture_tool": source_provenance[1],
            },
        }
        payload = json.dumps(descriptor, sort_keys=True, separators=(",", ":")) + "\n"
        _write_durable_bytes(staged / PACKAGE_DESCRIPTOR, payload.encode(), 0o640)
        _fsync_path(staged)
        return _install_staged_package(staged, out, descriptor)


def expected_package_files(descriptor: Mapping[str, Any]) -> frozenset[str]:
    files = set(descriptor["modules"]) | set(descriptor["dynamic_helpers"])
    files.update(item["package_file"] for item in descriptor["external_bindings"])
    files.add(PACKAGE_DESCRIPTOR)
    return frozenset(files)


def package_content_id(descriptor: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            {key: value for key, value in descriptor.items() if key != "root"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def verify_package_directory(
    root: Path, descriptor: dict[str, Any] | None = None
) -> dict[str, Any]:
    root = _canonical(root, "publisher closure root")
    if (root / INCOMPLETE_MARKER).exists() or (root / INCOMPLETE_MARKER).is_symlink():
        raise PublisherClosureError("publisher package publish is incomplete")
    if descriptor is None:
        descriptor_path = root / PACKAGE_DESCRIPTOR
        if not descriptor_path.is_file() or descriptor_path.is_symlink():
            raise PublisherClosureError("publisher package descriptor is missing")
        descriptor = validate(json.loads(descriptor_path.read_text()))
    if Path(descriptor["root"]) != root:
        raise PublisherClosureError("publisher package root differs from descriptor")
    inventory = expected_package_files(descriptor)
    present = {path.name for path in root.iterdir()}
    if present != inventory:
        raise PublisherClosureError("publisher package inventory differs")
    for name in inventory:
        if name == PACKAGE_DESCRIPTOR:
            path = root / name
            if path.is_symlink() or not path.is_file():
                raise PublisherClosureError("publisher package descriptor is missing")
            continue
        _file(root, name)
    return descriptor


def _install_staged_package(
    staged: Path, destination: Path, descriptor: dict[str, Any]
) -> dict[str, Any]:
    """Claim once; retain an unreadable partial package on any write failure.

    Removing the durable marker is the logical publication boundary. Neither
    capture nor publication renames over a directory another writer created.
    """
    destination = Path(destination)
    if (
        not destination.is_absolute()
        or destination.parent.resolve() != destination.parent
        or destination.is_symlink()
    ):
        raise PublisherClosureError("publisher package destination is not canonical")
    validate({**descriptor, "root": str(staged)})
    try:
        destination.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise PublisherClosureError("publisher package destination already exists") from exc
    marker = destination / INCOMPLETE_MARKER
    with marker.open("xb") as stream:
        stream.write(b"publisher-package-build/v1\n")
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_path(destination)
    _fsync_path(destination.parent)
    final = {**descriptor, "root": str(destination)}
    for name in sorted(expected_package_files(final) - {PACKAGE_DESCRIPTOR}):
        source = _file(staged, name)
        with (destination / name).open("xb") as stream:
            stream.write(source.read_bytes())
            stream.flush()
            os.fchmod(stream.fileno(), stat.S_IMODE(source.stat().st_mode))
            os.fsync(stream.fileno())
    payload = (json.dumps(final, sort_keys=True, separators=(",", ":")) + "\n").encode()
    with (destination / PACKAGE_DESCRIPTOR).open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fchmod(stream.fileno(), 0o640)
        os.fsync(stream.fileno())
    expected_pins = {**final["modules"], **final["dynamic_helpers"]}
    expected_pins.update({row["package_file"]: row["pin"] for row in final["external_bindings"]})
    if any(_pin(_file(destination, name)) != pin for name, pin in expected_pins.items()):
        raise PublisherClosureError("publisher package changed during publication")
    if {p.name for p in destination.iterdir()} != expected_package_files(final) | {
        INCOMPLETE_MARKER
    }:
        raise PublisherClosureError("publisher package inventory changed during publication")
    _fsync_path(destination)
    marker.unlink()
    _fsync_path(destination)
    _fsync_path(destination.parent)
    return verify_package_directory(destination)


def publish_package(
    source_root: Path,
    packages_root: Path,
    *,
    controller_data_root: Path,
    source_snapshot_receipt: Path,
    source_capture_tool: Path,
) -> dict[str, Any]:
    packages_root = Path(packages_root)
    packages_root.mkdir(parents=True, exist_ok=True)
    packages_root = _canonical(packages_root, "publisher packages root")
    with tempfile.TemporaryDirectory(dir=packages_root, prefix=".publisher-candidate-") as temp:
        staging = Path(temp) / "capture"
        descriptor = capture(
            source_root,
            staging,
            controller_data_root=controller_data_root,
            source_snapshot_receipt=source_snapshot_receipt,
            source_capture_tool=source_capture_tool,
        )
        package_id = package_content_id(descriptor)
        final = packages_root / package_id
        if final.exists() or final.is_symlink():
            existing = verify_package_directory(final)
            if package_content_id(existing) != package_id:
                raise PublisherClosureError("publisher package identity differs")
            return existing
        return _install_staged_package(staging, final, descriptor)


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
        top = fullname.partition(".")[0]
        if top not in set(sys.stdlib_module_names) | set(sys.builtin_module_names):
            raise PublisherClosureError("publisher attempted an unlisted non-stdlib import")
        cached = sys.modules.get(fullname)
        if cached is not None:
            origin = getattr(getattr(cached, "__spec__", None), "origin", None)
            if origin not in {"built-in", "frozen"} and not _stdlib_origin(origin):
                raise PublisherClosureError("publisher attempted a shadowed stdlib import")
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, [sysconfig.get_paths()["stdlib"]])
        origin = getattr(spec, "origin", None)
        if spec is None or (origin not in {"built-in", "frozen"} and not _stdlib_origin(origin)):
            raise PublisherClosureError("publisher attempted a shadowed stdlib import")
        return spec


def _stdlib_origin(origin: Any) -> bool:
    try:
        return (
            Path(str(origin))
            .resolve()
            .is_relative_to(Path(sysconfig.get_paths()["stdlib"]).resolve())
        )
    except (OSError, ValueError):
        return False


def load_entrypoint(
    value: Any,
) -> tuple[Callable[[list[str] | None], int], Callable[[], dict[str, Any]]]:
    descriptor = validate(value)
    root, data_root = Path(descriptor["root"]), Path(descriptor["controller_data_root"])
    names = (
        {Path(x).stem for x in (*descriptor["modules"], *descriptor["dynamic_helpers"])}
        | {item["module_name"] for item in descriptor["external_bindings"]}
        | {"_pinned_delivery_continuity"}
    )
    if names & set(sys.modules):
        raise PublisherClosureError("publisher closure module is already cached")
    sources = {
        Path(x).stem: ((root / x).read_bytes(), root / x, data_root / x)
        for x in (*descriptor["modules"], *descriptor["dynamic_helpers"])
    }
    for item in descriptor["external_bindings"]:
        sources[item["module_name"]] = (
            (root / item["package_file"]).read_bytes(),
            root / item["package_file"],
            Path(item["source_path"]),
        )
    # The bytes supplied to exec are the bytes validated above, not a second
    # unverified read from a mutable package path.
    pins_by_origin = {**descriptor["modules"], **descriptor["dynamic_helpers"]}
    pins_by_origin.update(
        {item["package_file"]: item["pin"] for item in descriptor["external_bindings"]}
    )
    for name, (payload, origin, _data_origin) in sources.items():
        expected = pins_by_origin[origin.name]["sha256"]
        if hashlib.sha256(payload).hexdigest() != expected:
            raise PublisherClosureError(f"publisher execution bytes differ: {name}")
    finder = _Finder(sources)
    owned_modules: dict[str, Any] = {}
    helper = data_root / DYNAMIC_HELPER
    external_by_pair = {
        (item["module_name"], item["source_path"]): item["module_name"]
        for item in descriptor["external_bindings"]
    }

    def guarded(name, location, *args, **kwargs):
        if Path(location) == helper and name == "_pinned_delivery_continuity":
            return importlib.machinery.ModuleSpec(
                name,
                _Loader(name, *sources[Path(DYNAMIC_HELPER).stem]),
                origin=str(root / DYNAMIC_HELPER),
            )
        external_name = external_by_pair.get((name, str(Path(location))))
        if external_name:
            return importlib.machinery.ModuleSpec(
                name, _Loader(name, *sources[external_name]), origin=str(sources[external_name][1])
            )
        raise PublisherClosureError("publisher attempted an unlisted dynamic import")

    @contextmanager
    def isolated():
        old_path, old_bytecode = list(sys.path), sys.dont_write_bytecode
        old_meta_path = list(sys.meta_path)
        old_spec = importlib.util.spec_from_file_location
        old_modules = {name: sys.modules[name] for name in names if name in sys.modules}
        if any(owned_modules.get(name) is not module for name, module in old_modules.items()):
            raise PublisherClosureError("publisher closure module is already cached")
        sys.modules.update(owned_modules)
        sys.path[:] = [x for x in sys.path if Path(x or ".").resolve() not in {root, data_root}]
        sys.meta_path.insert(0, finder)
        sys.dont_write_bytecode = True
        importlib.util.spec_from_file_location = guarded
        try:
            yield
        finally:
            owned_modules.update({name: sys.modules[name] for name in names if name in sys.modules})
            for name in names:
                if name in old_modules:
                    sys.modules[name] = old_modules[name]
                else:
                    sys.modules.pop(name, None)
            importlib.util.spec_from_file_location = old_spec
            sys.meta_path[:] = old_meta_path
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
