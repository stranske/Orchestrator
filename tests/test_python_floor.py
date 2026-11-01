"""Guard the supported Python metadata floor (owner decision: 3.13+)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import closer_mechanical_catalog as catalog


def test_python_floor_is_3_13() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    ruff = tomllib.loads((repo_root / "ruff.toml").read_text(encoding="utf-8"))
    assert ruff.get("target-version") == "py313"

    policies, _unmet = catalog.compile_catalog(repo_root)
    assert policies, "expected formatter/lint class policies from main source files"
