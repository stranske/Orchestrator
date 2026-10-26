"""Portable controls only; these never import installed Vibe or its SDK."""

import ast
import hashlib
import importlib.machinery
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
