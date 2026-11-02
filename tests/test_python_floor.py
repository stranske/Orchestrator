"""Guard the supported Python metadata floor (owner decision: 3.13+)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

import closer_mechanical_catalog as catalog


def test_python_floor_is_3_13(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    ruff = tomllib.loads((repo_root / "ruff.toml").read_text(encoding="utf-8"))
    assert ruff.get("target-version") == "py313"

    # compile_catalog raises ValueError("source_formatter_lint_rails_changed") when
    # ruff target-version is not py313; a successful return is the accept check.
    policies, unmet = catalog.compile_catalog(repo_root)
    assert policies, "expected formatter/lint class policies from main source files"
    assert unmet == {
        "docstring_fix": "source_command_and_independent_check_rail_UNMET",
    }

    # Reject path: same SOURCE_FILES with target-version rolled back to py312 must fail.
    # Without this, removing the catalog's py313 check would still leave the named
    # test green on main.
    for name in catalog.SOURCE_FILES:
        dest = tmp_path / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((repo_root / name).read_bytes())
    broken = tmp_path / "ruff.toml"
    text = broken.read_text(encoding="utf-8")
    assert 'target-version = "py313"' in text
    broken.write_text(
        text.replace('target-version = "py313"', 'target-version = "py312"', 1), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="source_formatter_lint_rails_changed"):
        catalog.compile_catalog(tmp_path)
