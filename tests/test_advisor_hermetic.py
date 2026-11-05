"""The capability-advisor suite imports against temporary runtime roots.

``import_under_temporary_runtime`` sets ``ORCH_STATE_DIR`` and ``ORCH_LOCAL_RUNTIME`` before the
import. Paths that import already captured are repointed for the test and restored afterwards.
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import advisor_hermetic

_modules = advisor_hermetic.import_under_temporary_runtime("capabilities", "repo_knowledge")
capabilities = _modules["capabilities"]
repo_knowledge = _modules["repo_knowledge"]

_FRESH_IMPORT = r"""
import json
import os
from pathlib import Path

import advisor_hermetic

before = {key: os.environ.get(key) for key in advisor_hermetic.ENV_KEYS}
base = advisor_hermetic.install_import_environment()
import capabilities
import repo_knowledge

during = {
    "state_env": os.environ["ORCH_STATE_DIR"],
    "runtime_env": os.environ["ORCH_LOCAL_RUNTIME"],
    "reg": str(capabilities.REG),
    "local": str(capabilities.LOCAL_RUNTIME),
    "save_default": str(capabilities.save.__defaults__[-1]),
    "knowledge_default": str(repo_knowledge.load.__defaults__[-1]),
}
advisor_hermetic.restore_import_environment()
after = {
    "reg": str(capabilities.REG),
    "local": str(capabilities.LOCAL_RUNTIME),
    "save_default": str(capabilities.save.__defaults__[-1]),
    "knowledge_reg": str(repo_knowledge.REG),
    "knowledge_default": str(repo_knowledge.load.__defaults__[-1]),
    "env": {key: os.environ.get(key) for key in advisor_hermetic.ENV_KEYS},
}
print(json.dumps({"before": before, "base": str(base), "during": during, "after": after}))
"""


def test_environment_is_assigned_before_the_advisor_import():
    """The import helper sets both roots before it loads capability_advisor."""
    installer = inspect.getsource(advisor_hermetic.install_import_environment)
    state_at = installer.index('os.environ["ORCH_STATE_DIR"]')
    runtime_at = installer.index('os.environ["ORCH_LOCAL_RUNTIME"]')
    assert state_at > 0 and runtime_at > 0
    loader = inspect.getsource(advisor_hermetic.import_under_temporary_runtime)
    assert loader.index("install_import_environment()") < loader.index("import_module")


def test_advisor_tests_use_temporary_runtime_roots_and_captured_defaults():
    """Already-captured ledger paths follow the temporary runtime for this test."""
    state = Path(os.environ["ORCH_STATE_DIR"])
    runtime = Path(os.environ["ORCH_LOCAL_RUNTIME"])
    base = advisor_hermetic.root()
    assert state == base / "state"
    assert runtime == base / "runtime"
    assert state != runtime
    assert capabilities.LOCAL_RUNTIME == runtime or capabilities.LOCAL_RUNTIME == runtime.resolve()
    assert capabilities.REG.name == "capabilities.json"
    assert capabilities.REG.is_relative_to(runtime) or capabilities.REG.is_relative_to(
        runtime.resolve()
    )
    assert capabilities.save.__defaults__[-1] == capabilities.REG
    assert capabilities.load_declared.__defaults__[-1] == capabilities.REG
    assert repo_knowledge.REG.name == "repo_knowledge.json"
    assert repo_knowledge.load.__defaults__[-1] == repo_knowledge.REG
    assert not capabilities.REG.is_relative_to(Path.home() / ".codex" / "orchestrator")


def test_fresh_import_binds_temporary_roots_then_restores_the_process():
    """A process that imports the advisor first must not keep the temporary paths."""
    repo = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(repo / "src"), str(repo / "tests"), env.get("PYTHONPATH", "")]
    )
    proc = subprocess.run(
        [sys.executable, "-c", _FRESH_IMPORT],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    base = Path(payload["base"])
    during = payload["during"]
    after = payload["after"]
    assert Path(during["state_env"]) == base / "state"
    assert Path(during["runtime_env"]) == base / "runtime"
    assert Path(during["reg"]).is_relative_to(base / "runtime")
    assert Path(during["local"]).is_relative_to(base / "runtime")
    assert Path(during["save_default"]).is_relative_to(base / "runtime")
    assert Path(during["knowledge_default"]).is_relative_to(base / "runtime")
    assert after["env"] == payload["before"]
    assert not str(after["reg"]).startswith(str(base))
    assert not str(after["local"]).startswith(str(base))
    assert after["save_default"] == after["reg"]
    assert after["knowledge_default"] == after["knowledge_reg"]
    assert not str(after["knowledge_default"]).startswith(str(base))


def test_changed_files_record_matches_the_suite():
    """The verification record names the test and fixture files this isolation changed."""
    repo = Path(__file__).resolve().parents[1]
    report = repo / "docs" / "verification" / "issue-1102" / "README.md"
    text = report.read_text(encoding="utf-8")
    assert "## Temporary runtime roots" in text
    for rel in advisor_hermetic.CHANGED_FILES:
        assert rel in text
        source = (repo / rel).read_text(encoding="utf-8")
        if rel == "tests/conftest.py":
            assert "isolated_advisor_runtime" in source
        elif rel == "tests/advisor_hermetic.py":
            assert "ORCH_STATE_DIR" in source and "ORCH_LOCAL_RUNTIME" in source
        else:
            assert "import_under_temporary_runtime" in source
