"""Exact nested replay wiring/ACT diff controls; no scheduler or provider run."""

import ast
import hashlib
import json
from pathlib import Path

import pytest
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
