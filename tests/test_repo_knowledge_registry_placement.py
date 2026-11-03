"""Real loader imports in both deployment shapes keep mutable data outside code."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import repo_knowledge


def _child(module_dir, home, runtime=None, registry=None, write_note=False):
    env = dict(os.environ, HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
    for name in ("ORCH_LOCAL_RUNTIME", "ORCH_REPO_KNOWLEDGE_PATH"):
        env.pop(name, None)
    if runtime is not None:
        env["ORCH_LOCAL_RUNTIME"] = str(runtime)
    if registry is not None:
        env["ORCH_REPO_KNOWLEDGE_PATH"] = str(registry)
    env["PYTHONPATH"] = os.pathsep.join(
        (str(module_dir), str(Path(repo_knowledge.__file__).parent))
    )
    code = """
import json, repo_knowledge
data = repo_knowledge.load()
if WRITE_NOTE:
    data['operator_note'] = 'preserve across generations'
    repo_knowledge.save(data)
repo_knowledge._capability_heartbeat = lambda: None
context = repo_knowledge.append_context('', 'stranske/Workflows#1')
print(json.dumps({'path': str(repo_knowledge.REG), 'note': data.get('operator_note'),
                  'context_present': bool(context)}))
""".replace("WRITE_NOTE", repr(write_note))
    return json.loads(subprocess.check_output([sys.executable, "-c", code], env=env, text=True))


@pytest.mark.parametrize("layout", ["flat", "src"])
@pytest.mark.parametrize("override", ["default", "runtime", "registry"])
def test_loader_writes_operational_registry_without_changing_module_tree(
    tmp_path, layout, override
):
    module_dir = tmp_path / "generation" / ("src" if layout == "src" else "modules")
    module_dir.mkdir(parents=True)
    shutil.copy2(repo_knowledge.__file__, module_dir / "repo_knowledge.py")
    before = {p.name: p.read_bytes() for p in module_dir.iterdir()}
    home = tmp_path / "home"
    runtime = tmp_path / "runtime" if override != "default" else None
    registry = tmp_path / "explicit.json" if override == "registry" else None
    expected = (
        registry
        or (runtime or home / ".codex" / "orchestrator") / "experiments/repo_knowledge.json"
    )
    result = _child(module_dir, home, runtime, registry)
    assert Path(result["path"]) == expected
    assert result["context_present"] is True
    assert expected.is_file()
    assert {p.name: p.read_bytes() for p in module_dir.iterdir()} == before


def test_two_generations_reuse_operational_registry_and_preserve_operator_data(tmp_path):
    home = tmp_path / "home"
    results = []
    for index in range(2):
        module_dir = tmp_path / f"generation-{index}"
        module_dir.mkdir()
        shutil.copy2(repo_knowledge.__file__, module_dir / "repo_knowledge.py")
        results.append(_child(module_dir, home, write_note=index == 0))
        assert not (module_dir / "experiments").exists()
    assert results[0]["path"] == results[1]["path"]
    assert results[1]["note"] == "preserve across generations"
