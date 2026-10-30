from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

import closer_publisher_closure as closure


@pytest.fixture(autouse=True)
def clear_publisher_modules():
    for name in ("controller", "demand_router"):
        sys.modules.pop(name, None)
    yield
    for name in ("controller", "demand_router"):
        sys.modules.pop(name, None)


def _pin(root: Path) -> dict:
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
    return {
        "schema": "closer-publisher-closure/v2",
        "root": str(root.resolve()),
        "controller_data_root": str(root.resolve()),
        "entry_module": "demand_router",
        "entry_function": "main",
        "modules": {name: modules[name] for name in closure.MODULES},
        "dynamic_helpers": {closure.DYNAMIC_HELPER: modules[closure.DYNAMIC_HELPER]},
        "capture": {
            "source_root": "/reviewed/source",
            "source_snapshot_receipt": {"path": "/reviewed/receipt", "sha256": "a" * 64},
            "source_capture_tool": {"path": "/reviewed/tool", "sha256": "b" * 64},
        },
    }


def test_valid_closure_loads_only_captured_modules(tmp_path):
    descriptor = _pin(tmp_path)
    entry, after = closure.load_entrypoint(descriptor)
    assert entry([]) == 3
    assert after() == descriptor | {
        "root": str(tmp_path.resolve()),
        "controller_data_root": str(tmp_path.resolve()),
        "modules": descriptor["modules"],
        "dynamic_helpers": descriptor["dynamic_helpers"],
    }


def test_changed_imported_bytes_refuse_before_entrypoint(tmp_path):
    descriptor = _pin(tmp_path)
    (tmp_path / "controller.py").write_text("VALUE = 9\n")
    with pytest.raises(closure.PublisherClosureError, match="bytes"):
        closure.load_entrypoint(descriptor)


def test_cached_local_module_refuses(tmp_path):
    descriptor = _pin(tmp_path)
    sys.modules["controller"] = object()  # type: ignore[assignment]
    try:
        with pytest.raises(closure.PublisherClosureError, match="cached"):
            closure.load_entrypoint(descriptor)
    finally:
        sys.modules.pop("controller", None)


def test_symlink_escape_refuses(tmp_path):
    descriptor = _pin(tmp_path)
    outside = tmp_path.parent / "outside.py"
    outside.write_text("VALUE=1\n")
    (tmp_path / "controller.py").unlink()
    (tmp_path / "controller.py").symlink_to(outside)
    with pytest.raises(closure.PublisherClosureError, match="escapes"):
        closure.validate(descriptor)


def test_after_detects_drift(tmp_path):
    descriptor = _pin(tmp_path)
    _entry, after = closure.load_entrypoint(descriptor)
    (tmp_path / "controller.py").write_text("VALUE = 8\n")
    with pytest.raises(closure.PublisherClosureError, match="bytes"):
        after()


def test_unlisted_nonstdlib_import_refuses(tmp_path):
    descriptor = _pin(tmp_path)
    router = tmp_path / "demand_router.py"
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
    descriptor = _pin(package)
    for name in (*closure.MODULES, closure.DYNAMIC_HELPER):
        (data / name).write_text("VALUE = 99\n")
    descriptor["controller_data_root"] = str(data.resolve())
    entry, _after = closure.load_entrypoint(descriptor)
    assert entry([]) == 3


def test_actual_controller_closure_is_capturable_without_running_a_publisher(tmp_path):
    """Model-free compatibility: inspect the real source, never import or execute it."""
    source = Path("/Users/teacher/.codex/automations/reviewed-repo-automation-controller")
    if not source.is_dir():
        pytest.skip("external publisher is unavailable in this checkout")
    output = tmp_path / "capture"
    receipt = tmp_path / "source-receipt.json"
    tool = tmp_path / "capture-tool.py"
    receipt.write_text("receipt")
    tool.write_text("tool")
    descriptor = closure.capture(
        source,
        output,
        controller_data_root=source,
        source_snapshot_receipt=receipt,
        source_capture_tool=tool,
    )
    assert closure.validate(descriptor)["modules"].keys() == set(closure.MODULES)
