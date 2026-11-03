"""Consumer seam regressions for the canonical, source-owned dependency auditor."""

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def audit(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1] / "scripts/sync_test_dependencies.py"
    spec = importlib.util.spec_from_file_location("consumer_dependency_audit", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "PYPROJECT_FILE", tmp_path / "pyproject.toml")
    return module


def test_nonpackage_exact_pins_are_authoritative(audit, tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length=100\n")
    (tmp_path / "requirements.lock").write_text("# direct pins\nPyYAML==6.0.3\npydantic==2.13.4\n")
    names, groups = audit.get_declared_dependencies()
    assert names == {"pyyaml", "pydantic"}
    assert groups == {"requirements.lock": ["PyYAML==6.0.3", "pydantic==2.13.4"]}


@pytest.mark.parametrize(
    "entry",
    [
        "-r other.txt",
        "package>=1",
        "package==1.*",
        "package[x]==1",
        'package==1; os_name=="posix"',
        "package @ https://example.invalid/p.whl",
    ],
)
def test_nonpackage_unsafe_or_inexact_declarations_refuse(audit, tmp_path, entry):
    (tmp_path / "requirements.lock").write_text(entry + "\n")
    with pytest.raises(audit.LockDeclarationError):
        audit.get_declared_dependencies()


def test_missing_lock_declares_nothing(audit):
    assert audit.get_declared_dependencies() == (set(), {})


def test_package_declarations_take_precedence_over_lock(audit, tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="fixture"\ndependencies=["requests>=2"]\n'
    )
    (tmp_path / "requirements.lock").write_text("-r forbidden.txt\n")
    names, _ = audit.get_declared_dependencies()
    assert names == {"requests"}


def test_nonpackage_fix_refuses_without_mutating_declarations(audit, tmp_path):
    project = tmp_path / "pyproject.toml"
    lock = tmp_path / "requirements.lock"
    project.write_text("[tool.ruff]\nline-length=100\n")
    lock.write_text("PyYAML==6.0.3\n")
    before = (project.read_bytes(), lock.read_bytes())
    with pytest.raises(SystemExit, match="requirements.lock maintenance process"):
        audit.add_dependencies_to_pyproject({"missing_dependency"}, fix=True)
    assert (project.read_bytes(), lock.read_bytes()) == before


def test_undeclared_import_is_still_a_failed_audit(audit, tmp_path, monkeypatch):
    (tmp_path / "requirements.lock").write_text("PyYAML==6.0.3\n")
    monkeypatch.setattr(audit, "get_all_test_imports", lambda: {"yaml", "undeclared_canary_73291"})
    monkeypatch.setattr(audit, "get_project_modules", set)
    assert audit.find_missing_dependencies() == {"undeclared_canary_73291"}
    assert audit.main(["--verify"]) == 1
