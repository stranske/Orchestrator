"""Temporary ORCH_STATE_DIR and ORCH_LOCAL_RUNTIME for capability-advisor tests.

`capabilities.REG`, `feedback.DB_PATH`, and the other runtime paths are bound when their
modules are first imported. A fixture that only calls ``monkeypatch.setenv`` runs after that
import, so it never moves them. Call ``import_under_temporary_runtime`` before importing
``capability_advisor``: it points both variables at a temporary directory for the import, then
puts the process environment and any paths that import captured back where they were, so the
rest of the suite keeps the roots it already bound. ``activated`` is what the advisor tests
run under; it setenvs the same directories again and repoints the captured paths for that test
only.
"""

from __future__ import annotations

import importlib
import inspect
import os
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

ENV_KEYS = (
    "ORCH_STATE_DIR",
    "ORCH_LOCAL_RUNTIME",
    "ORCH_CAPABILITIES_PATH",
    "ORCH_FEEDBACK_DB",
    "ORCH_FEATURES_PATH",
    "ORCH_REPO_KNOWLEDGE_PATH",
)
# Derived files an explicit env var outranks. A first import under the temporary runtime
# would otherwise keep the derived path after restore, hiding the variable the process had.
_EXPLICIT_RELATIVE = {
    "capabilities.json": "ORCH_CAPABILITIES_PATH",
    "feedback/orchestrator.db": "ORCH_FEEDBACK_DB",
    "experiments/features.json": "ORCH_FEATURES_PATH",
    "experiments/repo_knowledge.json": "ORCH_REPO_KNOWLEDGE_PATH",
}
# Imported while the temporary roots are set, so a later lazy import cannot bind the live ones.
_RUNTIME_MODULES = (
    "capabilities",
    "feedback",
    "features",
    "provision",
    "repo_knowledge",
    "fleet_shapes",
    "capability_advisor",
)
CHANGED_FILES = (
    "tests/advisor_hermetic.py",
    "tests/conftest.py",
    "tests/test_advisor_failure_boundaries.py",
    "tests/test_advisor_hermetic.py",
    "tests/test_advisor_live_rows.py",
    "tests/test_advisor_ranking_failure.py",
    "tests/test_capability_advisor_auto_decline.py",
    "tests/test_capability_advisor_cli_context.py",
    "tests/test_capability_advisor_coverage.py",
    "tests/test_capability_advisor_reach.py",
    "tests/test_capability_advisor_selftest_regressions.py",
)

_ROOT: Path | None = None
_DEPTH = 0
_SAVED: dict[str, str | None] | None = None


def root() -> Path:
    """The one temporary tree this process uses for advisor tests."""
    global _ROOT
    if _ROOT is None:
        base = Path(tempfile.mkdtemp(prefix="advisor-hermetic-"))
        (base / "state").mkdir()
        (base / "runtime" / "experiments").mkdir(parents=True)
        (base / "runtime" / "feedback").mkdir(parents=True)
        _ROOT = base
    return _ROOT


def install_import_environment() -> Path:
    """Point both runtime variables at ``root()`` before a capability-advisor import."""
    global _DEPTH, _SAVED
    base = root()
    if _DEPTH == 0:
        _SAVED = {key: os.environ.get(key) for key in ENV_KEYS}
        os.environ["ORCH_STATE_DIR"] = str(base / "state")
        os.environ["ORCH_LOCAL_RUNTIME"] = str(base / "runtime")
        for key in ENV_KEYS:
            if key not in ("ORCH_STATE_DIR", "ORCH_LOCAL_RUNTIME"):
                os.environ.pop(key, None)
    _DEPTH += 1
    return base


def restore_import_environment() -> None:
    """Undo ``install_import_environment``, including paths the import just bound."""
    global _DEPTH
    if _DEPTH == 0:
        return
    _DEPTH -= 1
    if _DEPTH != 0:
        return
    saved = _SAVED or {}
    try:
        _repoint(lambda path, name: _from_temporary(path, name, root(), saved), assign=setattr)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def import_under_temporary_runtime(*names: str) -> dict[str, Any]:
    """Import ``names`` (and the advisor's runtime modules) with the temporary roots set."""
    install_import_environment()
    try:
        loaded = {name: importlib.import_module(name) for name in _RUNTIME_MODULES}
        for name in names:
            loaded[name] = importlib.import_module(name)
        return loaded
    finally:
        restore_import_environment()


@contextmanager
def activated(monkeypatch: Any) -> Iterator[Path]:
    """Set the temporary roots for one test and repoint paths already bound at import."""
    base = root()
    state, runtime = base / "state", base / "runtime"
    captured_runtime, captured_state = _captured_roots()
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(runtime))
    for key in ENV_KEYS:
        if key not in ("ORCH_STATE_DIR", "ORCH_LOCAL_RUNTIME"):
            monkeypatch.delenv(key, raising=False)

    def translate(path: Path, name: str) -> Path | None:
        named = _named_runtime_path(name, path, runtime, state)
        if named is not None:
            return named
        return _between(path, captured_runtime, captured_state, runtime, state)

    _repoint(translate, assign=monkeypatch.setattr)
    yield base


def _default_root(saved: dict[str, str | None], key: str) -> Path:
    raw = saved.get(key)
    if raw:
        return Path(raw)
    return Path.home() / ".codex" / "orchestrator"


def _captured_roots() -> tuple[Path, Path]:
    capabilities = sys.modules.get("capabilities")
    runtime = getattr(capabilities, "LOCAL_RUNTIME", None) if capabilities is not None else None
    if not isinstance(runtime, Path):
        runtime = _default_root({}, "ORCH_LOCAL_RUNTIME")
    state = _default_root(
        {"ORCH_STATE_DIR": os.environ.get("ORCH_STATE_DIR")},
        "ORCH_STATE_DIR",
    )
    # Prefer a path a module already bound. ``os.environ`` may already be the temporary one.
    for module in sys.modules.values():
        bound = getattr(module, "STATE_DIR", None)
        if isinstance(bound, Path) and bound != runtime:
            state = bound
            break
        if isinstance(bound, Path):
            state = bound
    return runtime, state


def _module_dir() -> Path | None:
    capabilities = sys.modules.get("capabilities")
    file = getattr(capabilities, "__file__", None) if capabilities is not None else None
    if not file:
        return None
    return Path(file).resolve().parent


def _is_src(module: Any, src: Path) -> bool:
    file = getattr(module, "__file__", None)
    if not file:
        return False
    try:
        return Path(file).resolve().parent == src
    except OSError:
        return False


def _style(path: Path, destination: Path) -> Path:
    """Keep a resolved binding resolved, and leave an unresolved one unresolved."""
    try:
        if path.resolve() == path:
            return destination.expanduser().resolve()
    except OSError:
        return destination
    return destination


def _under(path: Path, root_path: Path) -> Path | None:
    if not path.is_absolute():
        return None
    try:
        if path == root_path or path.is_relative_to(root_path):
            return path.relative_to(root_path)
        resolved, resolved_root = path.resolve(), root_path.resolve()
        if resolved == resolved_root or resolved.is_relative_to(resolved_root):
            return resolved.relative_to(resolved_root)
    except (OSError, ValueError):
        return None
    return None


def _between(
    path: Path,
    src_runtime: Path,
    src_state: Path,
    dst_runtime: Path,
    dst_state: Path,
) -> Path | None:
    runtime_rel = _under(path, src_runtime)
    state_rel = _under(path, src_state)
    if runtime_rel is None and state_rel is None:
        return None
    same = src_runtime.resolve() == src_state.resolve()
    if same:
        if runtime_rel == Path("."):
            return None
        return _style(path, dst_runtime / runtime_rel)
    if runtime_rel is not None and (
        state_rel is None or len(src_runtime.resolve().parts) >= len(src_state.resolve().parts)
    ):
        return _style(path, dst_runtime / runtime_rel)
    if state_rel is not None:
        return _style(path, dst_state / state_rel)
    return None


def _named_runtime_path(name: str, path: Path, runtime: Path, state: Path) -> Path | None:
    """Attribute names that stay unambiguous even when the two live roots are one directory."""
    if name == "LOCAL_RUNTIME":
        return _style(path, runtime)
    if name == "STATE_DIR":
        return _style(path, state)
    if name == "DB_PATH":
        return _style(path, runtime / "feedback" / "orchestrator.db")
    if name == "FEATURES_REG" or (name == "REG" and path.name == "features.json"):
        return _style(path, runtime / "experiments" / "features.json")
    if name == "REG" and path.name == "repo_knowledge.json":
        return _style(path, runtime / "experiments" / "repo_knowledge.json")
    if name == "REG" and path.name == "capabilities.json":
        return _style(path, runtime / "capabilities.json")
    if name == "REPOS_DIR":
        return _style(path, runtime / "repos")
    if name == "WORKTREES_DIR":
        return _style(path, runtime / "worktrees")
    if name == "PROVISION_EVENTS_PATH":
        return _style(path, runtime / "provision-events.ndjson")
    if name == "DEFAULT_REPORT" and path.name == "redirect-sweep.json":
        return _style(path, state / "redirect-sweep.json")
    return None


def _from_temporary(
    path: Path,
    name: str,
    base: Path,
    saved: dict[str, str | None],
) -> Path | None:
    if _under(path, base / "runtime") is None and _under(path, base / "state") is None:
        return None
    original_runtime = _default_root(saved, "ORCH_LOCAL_RUNTIME")
    original_state = _default_root(saved, "ORCH_STATE_DIR")
    named = _named_runtime_path(name, path, original_runtime, original_state)
    relative = _under(path, base / "runtime")
    if relative is not None:
        key = _EXPLICIT_RELATIVE.get(relative.as_posix())
        explicit = saved.get(key) if key else None
        if explicit:
            return _style(path, Path(explicit))
    if named is not None and _under(path, base / "runtime") is not None:
        return named
    if named is not None and name in ("LOCAL_RUNTIME", "STATE_DIR"):
        return named
    return _between(path, base / "runtime", base / "state", original_runtime, original_state)


def _repoint(
    translate: Callable[[Path, str], Path | None],
    *,
    assign: Callable[[Any, str, Any], None],
) -> None:
    src = _module_dir()
    if src is None:
        return
    modules = [module for module in list(sys.modules.values()) if _is_src(module, src)]
    replacements: list[tuple[Any, str, Path]] = []
    # Defaults hold the Path object captured at import, not the attribute name. An old path that
    # two attributes send to different directories is left alone; the attributes themselves still move.
    default_map: dict[Path, Path | None] = {}
    for module in modules:
        for name, value in list(vars(module).items()):
            if name.startswith("__") or not isinstance(value, Path):
                continue
            new = translate(value, name)
            if not isinstance(new, Path) or new == value:
                continue
            replacements.append((module, name, new))
            if value in default_map and default_map[value] != new:
                default_map[value] = None
            elif value not in default_map:
                default_map[value] = new
    for module, name, new in replacements:
        assign(module, name, new)
    usable = {old: new for old, new in default_map.items() if new is not None}
    if not usable:
        return
    for module in modules:
        for value in list(vars(module).values()):
            if inspect.isfunction(value) and value.__module__ == module.__name__:
                _repoint_defaults(value, usable, assign)


def _repoint_defaults(
    func: Any,
    mapping: dict[Path, Path],
    assign: Callable[[Any, str, Any], None],
) -> None:
    defaults = func.__defaults__
    if defaults:
        updated = tuple(
            mapping.get(item, item) if isinstance(item, Path) else item for item in defaults
        )
        if updated != defaults:
            assign(func, "__defaults__", updated)
    kwdefaults = func.__kwdefaults__
    if not kwdefaults:
        return
    updated_kw = {
        key: mapping.get(item, item) if isinstance(item, Path) else item
        for key, item in kwdefaults.items()
    }
    if updated_kw != kwdefaults:
        assign(func, "__kwdefaults__", updated_kw)
