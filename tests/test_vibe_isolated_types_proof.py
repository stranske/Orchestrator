"""Portable controls only; these never import installed Vibe or its SDK."""

import ast
import hashlib
import importlib.machinery
import importlib.util
import os
import py_compile
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import vibe_isolated_types_proof as proof


def test_import_is_stdlib_only():
    tree = ast.parse(Path(proof.__file__).read_text())
    for node in tree.body:
        if isinstance(node, ast.Import):
            assert all(alias.name.split(".")[0] in sys.stdlib_module_names for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.module.split(".")[0] in sys.stdlib_module_names


@pytest.mark.parametrize(
    "name",
    [
        "mistralai",
        "httpx",
        "vibe.core.config",
        "vibe.core.tools",
        "vibe.core.agent_loop",
        "cwd_canary",
    ],
)
def test_forbidden_import_before_resolution(name, monkeypatch):
    def forbidden(*args):
        raise AssertionError("resolution must not run")

    monkeypatch.setattr(importlib.machinery.PathFinder, "find_spec", forbidden)
    with pytest.raises(ImportError, match="forbidden import"):
        proof.Guard({}).find_spec(name)


@pytest.mark.parametrize(
    "event",
    [
        "socket.__new__",
        "socket.connect",
        "socket.sendto",
        "subprocess.Popen",
        "os.system",
        "os.exec",
        "os.posix_spawn",
    ],
)
def test_audit_denies_before_external_action(event):
    with pytest.raises(PermissionError, match="blocked event"):
        proof.audit(event, (), {})


def test_home_or_unpinned_file_denied(tmp_path):
    path = tmp_path / "inert-not-a-credential"
    path.write_text("inert")
    with pytest.raises(PermissionError, match="non-code read"):
        proof.audit("open", (str(path), "r", 0), {})
    proof.audit("open", (str(path), "r", 0), {str(path.resolve()): proof.digest(path)})


def test_source_loader_ignores_stale_pyc_and_rejects_drift(tmp_path):
    source = tmp_path / "typing_extensions.py"
    source.write_text('VALUE = "source"\n')
    loader = proof.PinnedSource("typing_extensions", str(source))
    loader.expected = proof.digest(source)
    namespace = {}
    exec(loader.get_code("typing_extensions"), namespace)
    assert namespace["VALUE"] == "source"
    source.write_text('raise AssertionError("drift executed")\n')
    with pytest.raises(ImportError, match="source drift"):
        loader.get_code("typing_extensions")


def test_unpinned_dependency_fails_before_execution(tmp_path, monkeypatch):
    source = tmp_path / "typing_extensions.py"
    source.write_text('raise AssertionError("must not execute")\n')
    spec = importlib.machinery.ModuleSpec("typing_extensions", None, origin=str(source))
    monkeypatch.setattr(importlib.machinery.PathFinder, "find_spec", lambda *a: spec)
    with pytest.raises(ImportError, match="unreviewed import bytes"):
        proof.Guard({}).find_spec("typing_extensions")


def test_exact_ast_extraction_and_inventory_pruning(tmp_path):
    text = "class Boundary:\n    def action(self):\n        return 3\n\n    def unrelated(self):\n        raise RuntimeError\n"
    method = proof.extract(text, "Boundary", "action")
    assert method == "def action(self):\n    return 3\n"
    assert hashlib.sha256(method.encode()).hexdigest()
    for directory in ("site-packages", "__pycache__", "safe"):
        (tmp_path / directory).mkdir()
        (tmp_path / directory / "code.py").write_text("pass")
    assert list(proof.code_files(tmp_path)) == [tmp_path / "safe/code.py"]


def test_missing_isolation_refused_before_manifest_use():
    with pytest.raises(RuntimeError, match="fresh -I -S -B"):
        proof.child({})


def test_unknown_mode_never_spawns():
    with pytest.raises(ValueError, match="unknown mode"):
        proof.run_proof({}, "provider")


def test_bad_interpreter_never_spawns():
    with pytest.raises(ValueError, match="interpreter mismatch"):
        proof.run_proof({"interpreter": "/no-provider-cli"})


@pytest.mark.parametrize("mode,flags", [("w", 0), ("r+", 0), (None, 1)])
def test_pinned_code_cannot_be_overwritten(tmp_path, mode, flags):
    source = tmp_path / "code.py"
    source.write_text("pass")
    with pytest.raises(PermissionError, match="file mutation"):
        proof.audit("open", (str(source), mode, flags), {str(source): proof.digest(source)})


def test_actual_launcher_flags_and_clean_environment(monkeypatch):
    seen = []
    monkeypatch.setattr(proof, "digest", lambda path: "pin")
    monkeypatch.setenv("MISTRAL_API_KEY", "INERT_ENV_CANARY_NOT_CREDENTIAL")

    def inert_run(argv, **kwargs):
        seen.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(proof.subprocess, "run", inert_run)
    manifest = {
        "interpreter": str(proof.INTERPRETER),
        "interpreter_sha256": "pin",
        "files": {str(Path(proof.__file__).resolve()): "pin"},
    }
    result = proof.run_proof(manifest)
    argv, kwargs = seen[0]
    assert argv[1:5] == ["-I", "-S", "-B", "-X"]
    assert argv[5] == "pycache_prefix=" + str(kwargs["cwd"] / "empty-pycache")
    assert "MISTRAL_API_KEY" not in kwargs["env"]
    assert kwargs["env"]["PYDANTIC_DISABLE_PLUGINS"] == "__all__"
    assert kwargs["env"]["HOME"] == str(kwargs["cwd"])
    assert result["cache_files_after"] == []
    assert "INERT_ENV_CANARY" not in str(result)


@pytest.fixture
def method_manifest_tree(tmp_path, monkeypatch):
    """Only disposable source bytes; never inspect installed interpreter/packages."""
    packages = tmp_path / "packages"
    methods = packages / "vibe" / "core"
    (methods / "llm").mkdir(parents=True)
    files = {
        "agent_loop.py": "# synthetic method source\n",
        "llm/format.py": "# synthetic formatter source\n",
    }
    pins = {}
    for name, text in files.items():
        path = methods / name
        path.write_text(text)
        pins[name] = proof.digest(path)
    alias = tmp_path / "package-alias"
    alias.symlink_to(packages, target_is_directory=True)
    stdlib = tmp_path / "empty-stdlib"
    stdlib.mkdir()
    interpreter = tmp_path / "never-executed-interpreter"
    interpreter.write_bytes(b"synthetic pin only; never executed")
    monkeypatch.setattr(proof, "PACKAGES", alias)
    monkeypatch.setattr(proof, "STDLIB", stdlib)
    monkeypatch.setattr(proof, "INTERPRETER", interpreter)
    monkeypatch.setattr(proof, "METHOD_FILES", pins)
    monkeypatch.setattr(proof, "PUBLIC", ())
    monkeypatch.setattr(proof, "DEPENDENCIES", ())
    return alias, packages, pins


def test_method_manifest_uses_canonical_keys_for_symlinked_package_tree(method_manifest_tree):
    alias, packages, pins = method_manifest_tree
    manifest = proof.prepare_manifest()
    for name, expected in pins.items():
        aliased = alias / "vibe/core" / name
        canonical = (packages / "vibe/core" / name).resolve()
        assert str(aliased) not in manifest["files"]
        assert manifest["files"][str(canonical)] == expected
        # Directly exercise the same resolved lookup made by the audit hook.
        proof.audit("open", (str(aliased), "r", 0), manifest["files"])


def test_method_manifest_still_rejects_source_drift(method_manifest_tree):
    _alias, packages, _pins = method_manifest_tree
    (packages / "vibe/core/agent_loop.py").write_text("# changed source\n")
    with pytest.raises(ValueError, match="native method source drift"):
        proof.prepare_manifest()


def test_method_manifest_pin_does_not_authorize_retargeted_symlink(method_manifest_tree, tmp_path):
    alias, packages, pins = method_manifest_tree
    manifest = proof.prepare_manifest()
    replacement = tmp_path / "replacement"
    (replacement / "vibe/core/llm").mkdir(parents=True)
    for name in pins:
        (replacement / "vibe/core" / name).write_bytes((packages / "vibe/core" / name).read_bytes())
    alias.unlink()
    alias.symlink_to(replacement, target_is_directory=True)
    # Even identical bytes at a different canonical path were not reviewed.
    with pytest.raises(PermissionError, match="non-code read"):
        proof.audit("open", (str(alias / "vibe/core/agent_loop.py"), "r", 0), manifest["files"])


@pytest.mark.parametrize("symlinked", [False, True])
def test_real_pinned_find_spec_loads_verified_source_and_records_digest(tmp_path, symlinked):
    directory = tmp_path / "actual-modules"
    directory.mkdir()
    source = directory / "typing_extensions.py"
    source.write_text('VALUE = "stale"\n')
    before = source.stat()
    py_compile.compile(str(source), doraise=True)
    source.write_text('VALUE = "fresh"\n')  # Same length and timestamp: old pyc is eligible.
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    search = directory
    if symlinked:
        search = tmp_path / "module-alias"
        search.symlink_to(directory, target_is_directory=True)
    canonical = str(source.resolve())
    expected = proof.digest(source)
    guard = proof.Guard({canonical: expected})
    # Real PathFinder and real PinnedSource; no replacement finder/loader/guard.
    spec = guard.find_spec("typing_extensions", [str(search)])
    assert spec is not None and isinstance(spec.loader, proof.PinnedSource)
    assert spec.loader.expected == expected and spec.loader.path == canonical
    assert guard.loaded == {"typing_extensions": {"path": canonical, "sha256": expected}}
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.VALUE == "fresh"
    source.write_text('raise AssertionError("changed source must not execute")\n')
    with pytest.raises(ImportError, match="source drift before compilation"):
        spec.loader.exec_module(module)
