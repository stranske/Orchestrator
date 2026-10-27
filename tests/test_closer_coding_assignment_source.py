"""Detached selector plus actual prerun mechanics, not signed admission issuance.

No admission validator is executed, replaced or upgraded. Only a NEW inert local
Python A helper can run, after independent process-definition review.
"""

import ast
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import closer_runtime_binding as binding


def selected_source(home_source, admitted, context):
    """Execute only the existing post-validation selector AST as detached data."""
    tree = ast.parse(Path(binding.__file__).read_text())
    cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PinnedNativeHooks"
    )
    init = next(
        node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    candidates = []
    for node in ast.walk(init):
        if not isinstance(node, ast.If):
            continue
        text = ast.unparse(node.test)
        if text in {"'coding_continuation' in context", "'demand_assignment' in context"}:
            candidates.append(node)
    # Prefer the outer coding branch when present; baseline has demand only.
    selection = next(
        (node for node in candidates if "coding_continuation" in ast.unparse(node.test)),
        next(node for node in candidates if "demand_assignment" in ast.unparse(node.test)),
    )
    receiver = SimpleNamespace(assignment_source=home_source)
    namespace = {
        "self": receiver,
        "context": context,
        "admitted_assignment": admitted,
        "Path": Path,
    }
    exec(
        compile(
            ast.Module(body=[selection], type_ignores=[]), "<detached-source-selector>", "exec"
        ),
        namespace,
    )
    return receiver.assignment_source


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def mechanics(tmp_path):
    """Partial test object for real dependency/prerun methods; no source authority."""
    home = tmp_path / "synthetic-home"
    source = home / ".codex/automations/reviewed-repo-automation-controller/closer-assignment.json"
    source.parent.mkdir(parents=True)
    original = b'{"targets":[{"target":"synthetic/repository#12"}]}'
    source.write_bytes(original)
    admitted = tmp_path / "coding-assignment.json"
    admitted.write_text(json.dumps({"targets": [{"target": "synthetic/repository#9"}]}))
    context = {"coding_continuation": {"reference": "necessary-synthetic-selector-only"}}
    hooks = object.__new__(binding.PinnedNativeHooks)
    hooks.assignment_source = selected_source(source, admitted, context)
    hooks.bounded = True
    hooks.now = time.time
    hooks.interpreter_anchor = Path(sys.executable).resolve()
    dispatcher = tmp_path / "synthetic-dispatcher.py"
    dispatcher.write_text("# inert dependency, never executed\n")
    hooks.dispatcher_anchor = dispatcher
    hooks.dependencies = {
        "assignment": (admitted, digest(admitted)),
        "interpreter": (hooks.interpreter_anchor, digest(hooks.interpreter_anchor)),
        "dispatcher": (dispatcher, digest(dispatcher)),
    }
    hooks.lease_module = Path(binding.__file__).with_name("closer_native_lease.py")
    hooks.lease_digest = digest(hooks.lease_module)
    helper = tmp_path / "inert-A-helper"
    helper.write_text(
        "#!" + sys.executable + "\n"
        "import os\nfrom pathlib import Path\n"
        "root=Path(os.environ['HANDOFF_LANE_MUTEX_DIR'])\n"
        "root.mkdir()\n(root/'meta').write_text('inert-local-token')\n"
        "print('CLOSER_NATIVE_ADMISSION_V1 '+os.environ['CLOSER_ADMISSION_NONCE']+' ordinary')\n"
    )
    helper.chmod(0o700)
    hooks.scripts = {"prerun": helper, "postrun": helper}
    hooks.script_digests = {name: digest(helper) for name in hooks.scripts}
    hooks.lane = tmp_path / "owned-inert-lane"
    hooks.env = {
        "HOME": str(home),
        "ORCH_REDACT_SECRET_FILES": "",
        "PATH": "/usr/bin:/bin",
        "HANDOFF_LANE_MUTEX_DIR": str(hooks.lane),
    }
    hooks.runner = subprocess.run
    return hooks, source, original, admitted


def test_detached_coding_selection_allows_actual_prerun_with_home_unchanged(tmp_path):
    hooks, source, original, admitted = mechanics(tmp_path)
    assert hooks.assignment_source == admitted
    code, token = hooks.prerun(time.time() + 3)
    assert (code, token) == (0, "inert-local-token")
    assert source.read_bytes() == original
    assert json.loads(source.read_bytes())["targets"][0]["target"].endswith("#12")
    assert json.loads(admitted.read_bytes())["targets"][0]["target"].endswith("#9")


@pytest.mark.parametrize("change", ["bytes", "symlink", "pin"])
def test_actual_prerun_dependency_fences_remain_closed(tmp_path, change):
    hooks, source, original, admitted = mechanics(tmp_path)
    if change == "bytes":
        admitted.write_text("changed")
    elif change == "symlink":
        renamed = admitted.with_suffix(".retained")
        admitted.rename(renamed)
        admitted.symlink_to(renamed)
    else:
        hooks.dependencies["assignment"] = (admitted, "0" * 64)
    assert hooks.prerun(time.time() + 3) == (65, None)
    assert not hooks.lane.exists()
    assert source.read_bytes() == original


def test_detached_ordinary_and_demand_precedence_is_preserved(tmp_path):
    home, admitted, demand = (
        tmp_path / name for name in ("home.json", "admitted.json", "demand.json")
    )
    assert selected_source(home, admitted, {}) == home
    assert selected_source(home, admitted, {"demand_assignment": {"path": str(demand)}}) == demand
    assert (
        selected_source(
            home, admitted, {"coding_continuation": {}, "demand_assignment": {"path": str(demand)}}
        )
        == admitted
    )
