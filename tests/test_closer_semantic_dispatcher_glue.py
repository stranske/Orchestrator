"""Exact nested replay wiring/ACT diff controls; no scheduler or provider run."""

import ast
import copy
import hashlib
import json
from pathlib import Path

import pytest
from closer_semantic_audit_support import (
    fingerprint,
    original_act_merge,
    source_unchanged,
)
from closer_semantic_native_integration_support import (
    assert_reviewed_role_registry_successor,
    before_capacity_pre_effect_recovery_source,
)
from closer_semantic_native_integration_support import (
    integrated_reference as reference,
)
from test_closer_semantic_host_glue import candidate, disputed

import closer_dispatcher as closer


def replay_namespace(root):
    run = next(
        node
        for node in ast.parse(Path(closer.__file__).read_text()).body
        if isinstance(node, ast.FunctionDef) and node.name == "_run_staged_pipeline"
    )
    names = {
        "load_packet",
        "full_snapshot",
        "packet_evidence_root",
        "retained_worker_text",
        "CompletedResponseReadError",
        "completed_response_text",
        "full_interpretation",
    }
    nodes = [
        node
        for node in run.body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names
    ]
    assert {node.name for node in nodes} == names
    namespace = {**vars(closer), "retained_dir": root}
    exec(
        compile(
            ast.Module(body=nodes, type_ignores=[]), "actual-semantic-dispatcher-replay", "exec"
        ),
        namespace,
    )
    return namespace


def sealed_low(tmp_path, monkeypatch, output, snap, packet_ref, view):
    from test_closer_semantic_catalog_replay import sealed_input

    return sealed_input(tmp_path, monkeypatch, snap, packet_ref, view, output)


@pytest.mark.parametrize("damage", ["output_order", "inline_forged_selection"])
def test_actual_sealed_low_replay_reconstructs_same_host_candidate(tmp_path, monkeypatch, damage):
    case, value = disputed(tmp_path / "case")
    snap, packet, ref, root, *_ = case
    expected, chosen = candidate(case, value)
    assert chosen["status"] == "candidate"
    if damage == "output_order":
        value["obligations"].reverse()
    state = sealed_low(tmp_path / "receipt", monkeypatch, value, snap, ref, case[4])
    if damage == "inline_forged_selection":
        state["proposal"] = {
            "adjudication_request": {
                "basis": {"source": "foreign/issue#8", "criterion": "foreign"}
            },
            "semantic_followup": {"status": "candidate", "slot": 900},
        }
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps(snap))
    prep = {
        "snapshot": snap,
        "snapshot_ref": {
            "path": str(snapshot),
            "sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        },
        "revision": snap["revision"],
        "packet_ref": ref,
    }
    replay = replay_namespace(root)["full_interpretation"](state, snap["target"], prep)["proposal"]
    assert replay["adjudication_request"] == expected["adjudication_request"]
    assert replay["semantic_followup"] == chosen
    assert replay["semantic_assessment"] == value
    assert replay["authority"] is False


def functions(source):
    return {
        node.name: node
        for node in ast.parse(source).body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef))
    }


def test_dispatcher_ast_only_reserved_semantic_hunks_and_exact_one_act_merge():
    old = reference("9379f225")["files"]["closer_dispatcher.py"]
    source = before_capacity_pre_effect_recovery_source(
        Path(closer.__file__).read_bytes(), "closer_dispatcher.py"
    ).decode()
    new_top = functions(source)
    assert {
        name: value
        for name, value in old["top_level_ast"].items()
        if name not in {"_run_staged_pipeline", "_initial_dispatch_held", "main"}
    } == {
        name: fingerprint(node)
        for name, node in new_top.items()
        if name not in {"_run_staged_pipeline", "_initial_dispatch_held", "main"}
    }

    def nested(top):
        return {
            node.name: node
            for node in top["_run_staged_pipeline"].body
            if isinstance(node, (ast.FunctionDef, ast.ClassDef))
        }

    new = nested(new_top)
    allowed = {"interpret", "full_interpretation", "exception", "action_backend"}
    assert {
        name: value for name, value in old["nested_pipeline_ast"].items() if name not in allowed
    } == {name: fingerprint(node) for name, node in new.items() if name not in allowed}
    previous = original_act_merge()

    class RestoreMerge(ast.NodeTransformer):
        count = 0

        def visit_If(self, node):
            if (
                isinstance(node.test, ast.Call)
                and isinstance(node.test.func, ast.Name)
                and node.test.func.id == "isinstance"
                and "semantic_assessment" in ast.dump(node.test, include_attributes=False)
            ):
                self.count += 1
                return copy.deepcopy(previous)
            return self.generic_visit(node)

    restore = RestoreMerge()
    normalized = restore.visit(copy.deepcopy(new["action_backend"]))
    assert restore.count == 1
    assert fingerprint(normalized) == old["nested_pipeline_ast"]["action_backend"]
    # Strict STRONG grammar, host proof/authority validators and native/main/ACT
    # routing outside that single readback merge callsite remain unchanged.
    for filename in (
        "closer_execution_contracts.py",
        "closer_disposition.py",
        "closer_adjudication_policy.py",
    ):
        assert source_unchanged("9379f225", filename), filename

    # Exact reviewed native-registry data successor; validation function ASTs
    # remain unchanged, and the historical audit fixture is not rewritten.
    assert_reviewed_role_registry_successor()
