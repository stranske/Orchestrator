"""Prove synthesis_promotion binds runtime paths before import, not from shared state.

`feedback.LOCAL_RUNTIME`, `feedback.DB_PATH`, `capabilities.LOCAL_RUNTIME`, and
`capabilities.REG` are assigned while `synthesis_promotion` is imported. A later
`monkeypatch.setenv` cannot move those bindings. The child interpreter below
receives both temporary paths before its process starts.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_CHILD = r"""
import json
import os
from pathlib import Path

before = {
    "ORCH_STATE_DIR": os.environ["ORCH_STATE_DIR"],
    "ORCH_LOCAL_RUNTIME": os.environ.get("ORCH_LOCAL_RUNTIME"),
}
import synthesis_promotion  # noqa: F401
import capabilities
import feedback

bound = {
    "feedback_LOCAL_RUNTIME": str(feedback.LOCAL_RUNTIME),
    "feedback_DB_PATH": str(feedback.DB_PATH),
    "capabilities_LOCAL_RUNTIME": str(capabilities.LOCAL_RUNTIME),
    "capabilities_REG": str(capabilities.REG),
    "capabilities_FEATURES_REG": str(capabilities.FEATURES_REG),
}
os.environ["ORCH_LOCAL_RUNTIME"] = "/tmp/orch1109-too-late-runtime"
os.environ["ORCH_STATE_DIR"] = "/tmp/orch1109-too-late-state"
print(json.dumps({"before_import": before, "bound": bound, "after_env_change": {
    "feedback_LOCAL_RUNTIME": str(feedback.LOCAL_RUNTIME),
    "feedback_DB_PATH": str(feedback.DB_PATH),
    "capabilities_REG": str(capabilities.REG),
}}))
"""


def _child_env(state: Path, runtime: Path | None) -> dict[str, str]:
    repo = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    for key in ("ORCH_FEEDBACK_DB", "ORCH_CAPABILITIES_PATH", "ORCH_FEATURES_PATH"):
        env.pop(key, None)
    env["ORCH_STATE_DIR"] = str(state)
    if runtime is None:
        env.pop("ORCH_LOCAL_RUNTIME", None)
    else:
        env["ORCH_LOCAL_RUNTIME"] = str(runtime)
    env["PYTHONPATH"] = str(repo / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run_child(env: dict[str, str]) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD],
        cwd=Path(__file__).resolve().parents[1] / "src",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_synthesis_promotion_import_observes_temp_runtime_paths(tmp_path: Path) -> None:
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    state.mkdir()
    runtime.mkdir()
    payload = _run_child(_child_env(state, runtime))
    before = payload["before_import"]
    bound = payload["bound"]
    shared = Path.home() / ".codex" / "orchestrator"

    assert before["ORCH_STATE_DIR"] == str(state)
    assert before["ORCH_LOCAL_RUNTIME"] == str(runtime)
    assert Path(bound["feedback_LOCAL_RUNTIME"]) == runtime
    assert Path(bound["capabilities_LOCAL_RUNTIME"]) == runtime
    assert Path(bound["feedback_DB_PATH"]) == (runtime / "feedback" / "orchestrator.db").resolve()
    assert Path(bound["capabilities_REG"]) == runtime / "capabilities.json"
    assert Path(bound["capabilities_FEATURES_REG"]) == runtime / "experiments" / "features.json"
    assert shared not in Path(bound["feedback_DB_PATH"]).parents
    assert shared not in Path(bound["capabilities_REG"]).parents

    # Import already bound the paths. Changing the environment afterwards leaves them.
    assert payload["after_env_change"]["feedback_LOCAL_RUNTIME"] == str(runtime)
    assert payload["after_env_change"]["feedback_DB_PATH"] == bound["feedback_DB_PATH"]
    assert payload["after_env_change"]["capabilities_REG"] == str(runtime / "capabilities.json")


def test_state_dir_selects_the_feedback_store_when_runtime_is_unset(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    payload = _run_child(_child_env(state, None))
    before = payload["before_import"]
    bound = payload["bound"]
    shared = Path.home() / ".codex" / "orchestrator"

    assert before["ORCH_STATE_DIR"] == str(state)
    assert before["ORCH_LOCAL_RUNTIME"] is None
    assert Path(bound["feedback_DB_PATH"]) == (state / "feedback" / "orchestrator.db").resolve()
    assert Path(bound["feedback_LOCAL_RUNTIME"]) == shared
    assert Path(bound["capabilities_REG"]) == shared / "capabilities.json"
