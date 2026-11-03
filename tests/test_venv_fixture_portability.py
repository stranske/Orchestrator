"""Keep virtualenv fixtures executable with the current interpreter's shared libraries."""

import ast
import subprocess
import venv
from pathlib import Path


def test_symlinked_venv_interpreter_runs(tmp_path):
    root = tmp_path / ".venv"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(root)
    result = subprocess.run(
        [str(root / "bin/python"), "-c", "import sys"], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


def test_test_envbuilders_explicitly_use_symlinks():
    tests_dir = Path(__file__).parent
    violations = []
    for path in sorted(tests_dir.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "EnvBuilder" not in source:
            continue
        for node in ast.walk(ast.parse(source, filename=str(path))):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else node.func.id if isinstance(node.func, ast.Name) else None
            )
            if name == "EnvBuilder" and not any(
                keyword.arg == "symlinks"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in node.keywords
            ):
                violations.append(f"{path.relative_to(tests_dir)}:{node.lineno}")
    assert not violations, "EnvBuilder calls must set symlinks=True: " + ", ".join(violations)
