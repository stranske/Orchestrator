"""Pinned-environment preflight for the closer's verified release path.

This module deliberately does not replace :mod:`closer_release`.  It proves the
ambient assumptions that its verifier makes before handing control to its
existing journal and compare-and-swap installer.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from closer_release import ReleaseRecord

DEFAULT_PYTHON = Path("/opt/anaconda3/bin/python3")
DEFAULT_WORKERS = 4
_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_PROBE_TIMEOUT = 20


class PreflightError(RuntimeError):
    """A non-authorizing failure discovered before a release is begun."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ReleasePreflight:
    source: Path
    expected_source_sha: str
    mirror: Path
    core: Path
    expected_core_sha: str
    python: Path = DEFAULT_PYTHON
    journal: Path = Path("release.json")
    owner: str = "closer"
    workers: int = DEFAULT_WORKERS
    run_id: str | None = None
    timeout: float = 3600

    def normalized(self) -> "ReleasePreflight":
        return ReleasePreflight(
            source=_resolve_directory(self.source, "source_missing"),
            expected_source_sha=_sha(self.expected_source_sha, "source_sha_invalid"),
            mirror=Path(self.mirror).expanduser().absolute(),
            core=_resolve_directory(self.core, "core_missing"),
            expected_core_sha=_sha(self.expected_core_sha, "core_sha_invalid"),
            python=_resolve_executable(self.python),
            journal=Path(self.journal).expanduser().absolute(),
            owner=_required(self.owner, "owner_missing"),
            workers=_workers(self.workers),
            run_id=self.run_id,
            timeout=_timeout(self.timeout),
        )


def checked_environment(
    config: ReleasePreflight,
) -> tuple[ReleasePreflight, dict[str, str], dict[str, Any]]:
    """Validate immutable inputs and return the exact environment for the release.

    Nothing in this function writes a journal, mirror, snapshot, or installer
    artifact.  It is consequently safe to run repeatedly as a launch preflight.
    """
    started = time.monotonic()
    checked = config.normalized()
    _git_exact_clean(checked.source, checked.expected_source_sha, "source")
    _git_exact_clean(checked.core, checked.expected_core_sha, "core")
    runtime = checked.core / "scripts" / "closer_runtime.py"
    if not runtime.is_file():
        raise PreflightError("core_runtime_missing", f"missing required Core runtime: {runtime}")
    verifier = checked.source / "scripts" / "verify_before_sync.sh"
    installer = checked.source / "scripts" / "install_verified_snapshot.py"
    if not verifier.is_file() or not installer.is_file():
        raise PreflightError(
            "release_tools_missing", "source does not contain the verified release tools"
        )

    child = _inspect_python(checked.python)
    env = _release_environment(checked)
    _check_xdist(checked.python, env)
    receipt = {
        "ok": True,
        "phase": "preflight",
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "source": str(checked.source),
        "source_sha": checked.expected_source_sha,
        "core": str(checked.core),
        "core_sha": checked.expected_core_sha,
        "selected_python": str(checked.python),
        "child_python": child,
        "path_first": str(checked.python.parent),
        "workers": checked.workers,
    }
    return checked, env, receipt


def install_after_preflight(config: ReleasePreflight) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the existing green gate and CAS installer only after preflight succeeds."""
    checked, env, receipt = checked_environment(config)
    record = ReleaseRecord(checked.journal)
    previous = os.environ.copy()
    try:
        os.environ.clear()
        os.environ.update(env)
        result = record.verify_and_install(
            candidate_sha=checked.expected_source_sha,
            lane_owner=checked.owner,
            source=checked.source,
            mirror=checked.mirror,
            verifier=checked.source / "scripts" / "verify_before_sync.sh",
            installer=checked.source / "scripts" / "install_verified_snapshot.py",
            python=checked.python,
            bash="/bin/bash",
            run_id=checked.run_id,
            timeout=checked.timeout,
        )
    finally:
        os.environ.clear()
        os.environ.update(previous)
    return receipt, result


def _release_environment(config: ReleasePreflight) -> dict[str, str]:
    env = dict(os.environ)
    # Pin all subprocess resolution without changing a shared shell or shim.
    selected_dir = str(config.python.parent)
    path_parts = [part for part in env.get("PATH", "").split(os.pathsep) if part != selected_dir]
    env["PATH"] = os.pathsep.join([selected_dir, *path_parts])
    env["PYTHON"] = str(config.python)
    env["CLOSER_CORE_SOURCE"] = str(config.core)
    env["CLOSER_CORE_SOURCE_REVISION"] = config.expected_core_sha
    env["VERIFY_PYTEST_WORKERS"] = str(config.workers)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTEST_DISABLE_PLUGIN_AUTOLOAD", None)
    env.pop("CLOSER_CORE_PACKAGE", None)
    # Preserve the existing gate's refusal to inherit floor/shape relaxations.
    for name in (
        "VERIFY_BEFORE_SYNC_MAX_DIR_MB",
        "VERIFY_BEFORE_SYNC_TREE",
        "VERIFY_BEFORE_SYNC_FLOOR_MAY_LAG",
    ):
        env.pop(name, None)
    return env


def _inspect_python(python: Path) -> dict[str, Any]:
    child_probe = (
        "import json,sys; "
        "print(json.dumps({'executable':sys.executable,'version':list(sys.version_info[:3]),"
        "'prefix':sys.prefix,'base_prefix':sys.base_prefix,"
        "'base_executable':getattr(sys,'_base_executable',sys.executable)}))"
    )
    probe = (
        "import json, pathlib, subprocess, sys; "
        f"child=subprocess.check_output(['python3','-c',{child_probe!r}], text=True); "
        "print(json.dumps({'selected':str(pathlib.Path(sys.executable).resolve()),"
        "'version':list(sys.version_info[:3]),'prefix':sys.prefix,'base_prefix':sys.base_prefix,"
        "'base_executable':getattr(sys,'_base_executable',sys.executable),"
        "'child':json.loads(child)}))"
    )
    try:
        with tempfile.TemporaryDirectory(prefix="closer-release-preflight-home-") as fake_home:
            env = {"HOME": fake_home, "PATH": str(python.parent), "PYTHONNOUSERSITE": "1"}
            result = subprocess.run(
                [str(python), "-c", probe],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=_PROBE_TIMEOUT,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PreflightError(
            "python_probe_failed", f"selected Python could not run: {exc}"
        ) from exc
    if result.returncode:
        raise PreflightError(
            "python_probe_failed", result.stderr.strip() or "selected Python probe failed"
        )
    try:
        details = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PreflightError(
            "python_probe_invalid", "selected Python probe returned invalid JSON"
        ) from exc
    invocation = str(python.absolute())
    child = details.get("child", {})
    if not _same_executable(child.get("base_executable"), details.get("base_executable")):
        raise PreflightError(
            "python_child_mismatch", "fake-HOME child python3 did not resolve to selected Python"
        )
    if child.get("version") != details.get("version"):
        raise PreflightError(
            "python_version_mismatch",
            "fake-HOME child python3 version differs from selected Python",
        )
    if child.get("prefix") != details.get("prefix") or child.get("base_prefix") != details.get(
        "base_prefix"
    ):
        raise PreflightError(
            "python_environment_mismatch", "fake-HOME child lost selected Python environment"
        )
    return {
        "invocation": invocation,
        "executable": str(python.resolve()),
        "base_executable": details["base_executable"],
        "version": details["version"],
        "prefix": details["prefix"],
        "base_prefix": details["base_prefix"],
        "fake_home": True,
    }


def _same_executable(first: object, second: object) -> bool:
    try:
        return (
            isinstance(first, str) and isinstance(second, str) and os.path.samefile(first, second)
        )
    except OSError:
        return False


def _check_xdist(python: Path, env: dict[str, str]) -> None:
    try:
        result = subprocess.run(
            [str(python), "-m", "pytest", "--help"],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PreflightError(
            "xdist_probe_failed", f"selected Python could not run pytest help: {exc}"
        ) from exc
    if result.returncode or "-n numprocesses" not in result.stdout:
        raise PreflightError(
            "xdist_unavailable", "selected Python pytest does not accept pytest-xdist -n"
        )


def _git_exact_clean(path: Path, expected: str, label: str) -> None:
    try:
        head = _git(path, "rev-parse", "HEAD")
        dirty = _git(path, "status", "--porcelain", "--untracked-files=all")
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise PreflightError(f"{label}_not_git", f"{label} is not a readable git checkout") from exc
    if head != expected:
        raise PreflightError(
            f"{label}_sha_mismatch", f"{label} HEAD does not match its expected SHA"
        )
    if dirty:
        raise PreflightError(f"{label}_dirty", f"{label} has uncommitted or untracked files")


def _git(path: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(path), *args], text=True, timeout=_PROBE_TIMEOUT
    ).strip()


def _resolve_directory(value: Path | str, code: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_dir():
        raise PreflightError(code, f"required directory does not exist: {path}")
    return path.resolve()


def _resolve_executable(value: Path | str) -> Path:
    path = Path(value).expanduser()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise PreflightError("python_missing", f"selected Python is not executable: {path}")
    # Resolving a venv's interpreter symlink here selects its base Python and
    # discards the explicitly selected environment before the child probe.
    return path.absolute()


def _sha(value: str, code: str) -> str:
    if not _SHA.fullmatch(value):
        raise PreflightError(code, "expected SHA must be a lowercase 40 or 64 character git SHA")
    return value


def _workers(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PreflightError("workers_invalid", "workers must be at least one")
    return value


def _required(value: str, code: str) -> str:
    if not value:
        raise PreflightError(code, "owner is required")
    return value


def _timeout(value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise PreflightError("timeout_invalid", "timeout must be positive")
    return value


def error_receipt(exc: PreflightError, elapsed: float) -> dict[str, Any]:
    return {
        "ok": False,
        "phase": "preflight",
        "elapsed_seconds": round(elapsed, 3),
        "error": {"code": exc.code, "message": str(exc)},
    }


def selftest() -> dict[str, Any]:
    """Small no-write contract check for the stable CLI."""
    assert _sha("a" * 40, "x") == "a" * 40
    assert _sha("b" * 64, "x") == "b" * 64
    assert _workers(DEFAULT_WORKERS) == DEFAULT_WORKERS
    assert _timeout(1) == 1
    return {"ok": True, "phase": "selftest"}
