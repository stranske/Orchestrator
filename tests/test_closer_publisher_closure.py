from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

import closer_publisher_closure as closure
from paths import MODULE_DIR, checkout_root

REPO = checkout_root(MODULE_DIR)
REAL_RECEIPT = Path(
    "/private/tmp/closer-goal-repair-20261029/release-source-bounded-diagnostics.json"
)


@pytest.fixture(autouse=True)
def clear_publisher_modules():
    names = [
        "controller",
        "demand_router",
        "accountability_guard",
        "maintenance_campaign_budget",
        "maintenance_owner_lock",
        "_pinned_delivery_continuity",
    ]
    for name in names:
        sys.modules.pop(name, None)
    yield
    for name in names:
        sys.modules.pop(name, None)


def _minimal_verified_source(tmp_path: Path) -> tuple[Path, Path]:
    snapshot = tmp_path / "verified-snapshot"
    snapshot.mkdir()
    shutil.copy2(REPO / "orchestrate.sh", snapshot / "orchestrate.sh")
    (snapshot / "scripts").mkdir()
    shutil.copy2(
        REPO / "scripts" / "install_verified_snapshot.py",
        snapshot / "scripts" / "install_verified_snapshot.py",
    )
    shutil.copy2(
        MODULE_DIR / closure.CAPTURE_IMPLEMENTATION, snapshot / closure.CAPTURE_IMPLEMENTATION
    )
    (snapshot / "stub.py").write_text("VALUE = 0\n")
    digest = closure._snapshot_digest_fn()(snapshot)
    receipt_path = tmp_path / "source-receipt.json"
    receipt_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate_sha": "a" * 40,
                "incumbent_token": "generation-v1:" + "b" * 64,
                "lane_owner": "publisher-closure-test",
                "revision": 1,
                "stage": "verified",
                "verification": {
                    "verified": True,
                    "exit_code": 0,
                    "snapshot_path": str(snapshot.resolve()),
                    "snapshot_digest": digest,
                    "source_identity": "c" * 40,
                    "log_path": str((tmp_path / "verification.log").resolve()),
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    return receipt_path, snapshot


def _pin(root: Path, tmp_path: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    data_root = tmp_path / "operational-data"
    data_root.mkdir(exist_ok=True)
    files = {name: "VALUE = 3\n" for name in closure.MODULES}
    files["demand_router.py"] = "import controller\ndef main(argv=None): return controller.VALUE\n"
    files[closure.DYNAMIC_HELPER] = "VALUE = 4\n"
    for name, text in files.items():
        (root / name).write_text(text)
        os.chmod(root / name, 0o640)
    modules = {
        name: {"sha256": hashlib.sha256((root / name).read_bytes()).hexdigest(), "mode": 0o640}
        for name in files
    }
    external = []
    for module_name, source_path in closure.EXTERNAL_BINDINGS:
        package_file = "external-" + module_name + ".py"
        # Unit fixtures remain portable; the separate actual-source test loads
        # the real declared graph on hosts where that publisher is available.
        (root / package_file).write_text("VALUE = 0\n")
        os.chmod(root / package_file, 0o640)
        external.append(
            {
                "module_name": module_name,
                "source_path": source_path,
                "package_file": package_file,
                "pin": {
                    "sha256": hashlib.sha256((root / package_file).read_bytes()).hexdigest(),
                    "mode": 0o640,
                },
            }
        )
    receipt_path, _snapshot = _minimal_verified_source(tmp_path)
    receipt_binding, tool_binding = closure._bind_source_provenance(
        receipt_path, MODULE_DIR / closure.CAPTURE_IMPLEMENTATION
    )
    return {
        "schema": "closer-publisher-closure/v2",
        "root": str(root.resolve()),
        "controller_data_root": str(data_root.resolve()),
        "entry_module": "demand_router",
        "entry_function": "main",
        "modules": {name: modules[name] for name in closure.MODULES},
        "dynamic_helpers": {closure.DYNAMIC_HELPER: modules[closure.DYNAMIC_HELPER]},
        "external_bindings": external,
        "capture": {
            "source_root": str(root.resolve()),
            "source_snapshot_receipt": receipt_binding,
            "source_capture_tool": tool_binding,
        },
    }


def test_valid_closure_loads_only_captured_modules(tmp_path):
    descriptor = _pin(tmp_path / "pkg", tmp_path)
    entry, after = closure.load_entrypoint(descriptor)
    assert entry([]) == 3
    assert after() == descriptor | {
        "root": str((tmp_path / "pkg").resolve()),
        "controller_data_root": str((tmp_path / "operational-data").resolve()),
        "modules": descriptor["modules"],
        "dynamic_helpers": descriptor["dynamic_helpers"],
    }


def test_changed_imported_bytes_refuse_before_entrypoint(tmp_path):
    root = tmp_path / "pkg"
    descriptor = _pin(root, tmp_path)
    (root / "controller.py").write_text("VALUE = 9\n")
    with pytest.raises(closure.PublisherClosureError, match="bytes"):
        closure.load_entrypoint(descriptor)


def test_mutable_data_root_cannot_overlap_code_package(tmp_path):
    descriptor = _pin(tmp_path / "package", tmp_path)
    descriptor["controller_data_root"] = descriptor["root"]
    with pytest.raises(closure.PublisherClosureError, match="roots overlap"):
        closure.validate(descriptor)


def test_cached_local_module_refuses(tmp_path):
    descriptor = _pin(tmp_path / "pkg", tmp_path)
    sys.modules["controller"] = object()  # type: ignore[assignment]
    try:
        with pytest.raises(closure.PublisherClosureError, match="cached"):
            closure.load_entrypoint(descriptor)
    finally:
        sys.modules.pop("controller", None)


def test_load_and_lazy_calls_restore_process_import_state(tmp_path):
    root = tmp_path / "package"
    descriptor = _pin(root, tmp_path)
    router = root / "demand_router.py"
    router.write_text("def main(argv=None):\n    import controller\n    return controller.VALUE\n")
    descriptor["modules"][router.name]["sha256"] = hashlib.sha256(router.read_bytes()).hexdigest()
    before = (list(sys.path), list(sys.meta_path), sys.dont_write_bytecode)
    spec_function = importlib.util.spec_from_file_location
    entry, _after = closure.load_entrypoint(descriptor)
    assert "demand_router" not in sys.modules and "controller" not in sys.modules
    assert entry([]) == 3 and entry([]) == 3
    assert "demand_router" not in sys.modules and "controller" not in sys.modules
    assert (sys.path, sys.meta_path, sys.dont_write_bytecode) == before
    assert importlib.util.spec_from_file_location is spec_function


def test_symlink_escape_refuses(tmp_path):
    root = tmp_path / "pkg"
    descriptor = _pin(root, tmp_path)
    outside = tmp_path.parent / "outside.py"
    outside.write_text("VALUE=1\n")
    (root / "controller.py").unlink()
    (root / "controller.py").symlink_to(outside)
    with pytest.raises(closure.PublisherClosureError, match="escapes"):
        closure.validate(descriptor)


def test_after_detects_drift(tmp_path):
    root = tmp_path / "pkg"
    descriptor = _pin(root, tmp_path)
    _entry, after = closure.load_entrypoint(descriptor)
    (root / "controller.py").write_text("VALUE = 8\n")
    with pytest.raises(closure.PublisherClosureError, match="bytes"):
        after()


def test_unlisted_nonstdlib_import_refuses(tmp_path):
    root = tmp_path / "pkg"
    descriptor = _pin(root, tmp_path)
    router = root / "demand_router.py"
    router.write_text("import unlisted_publisher_dependency\ndef main(argv=None): return 0\n")
    descriptor["modules"]["demand_router.py"]["sha256"] = hashlib.sha256(
        router.read_bytes()
    ).hexdigest()
    with pytest.raises(closure.PublisherClosureError, match="unlisted"):
        closure.load_entrypoint(descriptor)


def test_entry_uses_captured_bytes_but_legacy_file_points_to_data_root(tmp_path):
    package = tmp_path / "package"
    data = tmp_path / "data"
    package.mkdir()
    data.mkdir()
    descriptor = _pin(package, tmp_path)
    for name in (*closure.MODULES, closure.DYNAMIC_HELPER):
        (data / name).write_text("VALUE = 99\n")
    descriptor["controller_data_root"] = str(data.resolve())
    entry, _after = closure.load_entrypoint(descriptor)
    assert entry([]) == 3


def test_loader_restores_import_state(tmp_path):
    root = tmp_path / "pkg"
    descriptor = _pin(root, tmp_path)
    old_path = list(sys.path)
    old_meta = list(sys.meta_path)
    entry, _after = closure.load_entrypoint(descriptor)
    entry([])
    assert sys.path == old_path
    assert sys.meta_path == old_meta


def test_finder_rejects_ambient_stdlib_shadow(tmp_path, monkeypatch):
    shadow = tmp_path / "fractions.py"
    shadow.write_text("raise AssertionError('ambient')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "fractions", raising=False)
    finder = closure._Finder({})
    assert finder.find_spec("fractions").origin != str(shadow)


def test_finder_rejects_cached_fake_stdlib(monkeypatch):
    class Fake:
        __spec__ = type("Spec", (), {"origin": "/tmp/fractions.py"})()

    monkeypatch.setitem(sys.modules, "fractions", Fake())
    with pytest.raises(closure.PublisherClosureError, match="shadowed"):
        closure._Finder({}).find_spec("fractions")


def test_fabricated_source_receipt_refuses(tmp_path):
    root = tmp_path / "pkg"
    descriptor = _pin(root, tmp_path)
    descriptor["capture"]["source_snapshot_receipt"]["snapshot_digest"] = "f" * 64
    with pytest.raises(closure.PublisherClosureError, match="verified|digest"):
        closure.validate(descriptor)


def test_fabricated_capture_tool_refuses(tmp_path):
    root = tmp_path / "pkg"
    descriptor = _pin(root, tmp_path)
    descriptor["capture"]["source_capture_tool"]["implementation_sha256"] = "a" * 64
    with pytest.raises(closure.PublisherClosureError, match="implementation|member"):
        closure.validate(descriptor)


def test_legacy_provenance_shape_refuses(tmp_path):
    root = tmp_path / "pkg"
    root.mkdir()
    descriptor = _pin(root, tmp_path)
    descriptor["capture"]["source_snapshot_receipt"] = {"path": "/x", "sha256": "a" * 64}
    with pytest.raises(closure.PublisherClosureError, match="provenance"):
        closure.validate(descriptor)


def test_publish_package_is_create_only_and_self_consistent(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    files = {name: "VALUE = 1\n" for name in closure.MODULES}
    files["demand_router.py"] = "import controller\ndef main(argv=None): return controller.VALUE\n"
    files[closure.DYNAMIC_HELPER] = "VALUE = 4\n"
    for name, text in files.items():
        (source / name).write_text(text)
        os.chmod(source / name, 0o640)
    bindings = []
    for name, _path in closure.EXTERNAL_BINDINGS:
        path = tmp_path / f"{name}.py"
        path.write_text("VALUE = 0\n")
        bindings.append((name, str(path)))
    monkeypatch.setattr(closure, "EXTERNAL_BINDINGS", tuple(bindings))
    receipt_path, _snapshot = _minimal_verified_source(tmp_path)
    packages_root = tmp_path / "packages"
    packages_root.mkdir()
    tool = MODULE_DIR / closure.CAPTURE_IMPLEMENTATION
    first = closure.publish_package(
        source,
        packages_root,
        controller_data_root=source,
        source_snapshot_receipt=receipt_path,
        source_capture_tool=tool,
    )
    second = closure.publish_package(
        source,
        packages_root,
        controller_data_root=source,
        source_snapshot_receipt=receipt_path,
        source_capture_tool=tool,
    )
    assert first == second
    package_root = Path(first["root"])
    before = (package_root / closure.PACKAGE_DESCRIPTOR).read_text()
    assert closure.verify_package_directory(package_root) == first
    assert (package_root / closure.PACKAGE_DESCRIPTOR).read_text() == before


def test_exclusive_publish_preserves_raced_foreign_empty_directory(tmp_path, monkeypatch):
    staged = tmp_path / "staged"
    descriptor = _pin(staged, tmp_path)
    target = tmp_path / "target"
    original = Path.mkdir

    def create_foreign_before_claim(path, *args, **kwargs):
        if path == target:
            original(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", create_foreign_before_claim)
    with pytest.raises(closure.PublisherClosureError, match="already exists"):
        closure._install_staged_package(staged, target, descriptor)
    assert target.is_dir() and list(target.iterdir()) == []


def test_failed_publish_retains_marker_and_refuses_loading(tmp_path, monkeypatch):
    staged = tmp_path / "staged"
    descriptor = _pin(staged, tmp_path)
    target = tmp_path / "target"
    original = Path.open

    def fail_copy(path, *args, **kwargs):
        if path == target / "controller.py" and args[:1] == ("xb",):
            raise OSError("injected write failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_copy)
    with pytest.raises(OSError, match="injected"):
        closure._install_staged_package(staged, target, descriptor)
    assert (target / closure.INCOMPLETE_MARKER).is_file()
    with pytest.raises(closure.PublisherClosureError, match="incomplete"):
        closure.validate({**descriptor, "root": str(target)})
    with pytest.raises(closure.PublisherClosureError, match="incomplete"):
        closure.verify_package_directory(target)


def test_package_verifier_rejects_unlisted_directory(tmp_path):
    root = tmp_path / "package"
    descriptor = _pin(root, tmp_path)
    (root / closure.PACKAGE_DESCRIPTOR).write_text(json.dumps(descriptor))
    (root / "unlisted").mkdir()
    with pytest.raises(closure.PublisherClosureError, match="inventory"):
        closure.verify_package_directory(root)


def test_actual_controller_closure_is_capturable_without_running_a_publisher(tmp_path, monkeypatch):
    """Import the actual graph without executing the publisher entrypoint."""
    source = Path(
        os.environ.get(
            "CLOSER_PUBLISHER_TEST_SOURCE",
            "/Users/teacher/.codex/automations/reviewed-repo-automation-controller",
        )
    )
    if not source.is_dir():
        pytest.skip("external publisher is unavailable in this checkout")
    data_root = Path(os.environ.get("CLOSER_PUBLISHER_TEST_DATA_ROOT", str(source)))
    receipt, _snapshot = _minimal_verified_source(tmp_path)
    tool = MODULE_DIR / closure.CAPTURE_IMPLEMENTATION
    output = tmp_path / "capture"
    descriptor = closure.capture(
        source,
        output,
        controller_data_root=data_root,
        source_snapshot_receipt=receipt,
        source_capture_tool=tool,
    )
    assert closure.validate(descriptor)["modules"].keys() == set(closure.MODULES)
    origins = {}
    original = closure._Loader.exec_module

    def record_origin(loader, module):
        original(loader, module)
        origins[loader.name] = (module.__file__, module.__spec__.origin)

    monkeypatch.setattr(closure._Loader, "exec_module", record_origin)
    entry, _after = closure.load_entrypoint(descriptor)
    assert callable(entry)
    assert origins["demand_router"] == (
        str(data_root / "demand_router.py"),
        str(output / "demand_router.py"),
    )
    assert "demand_router" not in sys.modules
