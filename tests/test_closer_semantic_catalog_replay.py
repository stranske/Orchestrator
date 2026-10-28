"""Exact catalog view is tied to actual synthetic controlled physical receipt."""

import ast
import hashlib
import json
from pathlib import Path

import pytest
from test_closer_controlled_origin import proof as component_proof
from test_closer_controlled_origin import validate
from test_closer_semantic_assessment import answer, block, prompt, setup

import closer_data_only_execution as execution
import closer_dispatcher as closer
import closer_semantic_assessment as semantic
import credential_redaction
import dispatcher
import transport_saved_result


@pytest.fixture(autouse=True)
def no_live_known_secret_files(monkeypatch):
    monkeypatch.setenv("ORCH_REDACT_SECRET_FILES", "")


def actual_replay(root):
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
        compile(ast.Module(body=nodes, type_ignores=[]), "actual-catalog-bound-replay", "exec"),
        namespace,
    )
    return namespace["full_interpretation"]


def sealed_input(tmp_path, monkeypatch, snap, packet_ref, view, output):
    tmp_path.mkdir()
    proof = component_proof.__wrapped__(tmp_path)
    result, expected = proof.result, proof.expected
    origin = result["data_only_origin"]
    rendered = prompt(snap, view)
    wire_view = block(rendered, "CURRENT_HOST_INVENTORY")
    assert wire_view == view
    cfg, binding = origin["execution"]["config"], origin["execution"]["binding"]
    limits = expected["limits"]
    env = execution.isolated_environment(
        cfg["private_root"],
        cwd=cfg["cwd"],
        existing_oauth_token="synthetic-only-no-read",
        limits=limits,
    )
    launch = execution.validate_launch(
        agent="claude",
        profile=expected["profile_identity"],
        binary_path=binding["binary_path"],
        binary_sha256=binding["binary_sha256"],
        argv=execution.expected_argv(
            binding["binary_path"], expected["profile_identity"], limits=limits
        ),
        env=env,
        cwd=cfg["cwd"],
        private_root=cfg["private_root"],
        response_protocol=execution.NATIVE_JSON,
        source_sha256=binding["source_sha256"],
        limits=limits,
        prompt=rendered,
        timeout_seconds=20,
    )
    expected["prompt"] = execution.validate_payload(rendered, limits=limits, kind="prompt")
    expected["basis"] = {
        "target": snap["target"],
        "revision": snap["revision"],
        "head": snap["head"],
        "base": snap["base"],
        "packet_sha256": packet_ref["sha256"],
        "semantic_catalog_sha256": semantic.catalog_digest(wire_view),
    }
    expected["finite_frontier_target"] = origin["finite_frontier_target"] = snap["target"]
    origin["execution"] = launch
    for observed in [
        result["process_observation"],
        origin["process_observation"],
        *proof.observed.values(),
    ]:
        observed.update(
            finite_frontier_target=snap["target"],
            data_only_execution_binding_sha256=launch["binding_sha256"],
        )
    result["output"] = json.dumps(output)
    native = json.loads(origin["native_json"])
    native["result"] = result["output"]
    origin["native_json"] = json.dumps(native)
    origin["native_stdout_sha256"] = hashlib.sha256(origin["native_json"].encode()).hexdigest()
    origin["native_stdout_bytes"] = len(origin["native_json"].encode())
    origin["final"] = execution.validate_payload(result["output"], limits=limits, kind="final")
    origin["normalized_native_result"] = transport_saved_result.normalize_claude_result(
        origin["native_json"],
        run_id=result["run_id"],
        pool_id="claude-subscription",
        requested_model=expected["profile_identity"]["requested_model"],
        requested_effort=expected["profile_identity"]["reasoning_effort"],
    )
    proof.retain()
    checked = validate(proof)
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", tmp_path)
    return {
        "requested_profile": expected["profile_id"],
        "requested_agent": "claude",
        "actual": {"exit": 0, "run_id": result["run_id"], "_closer_controlled_proof": checked},
    }


@pytest.mark.parametrize("drift", [False, True])
def test_exact_rendered_view_is_required_by_real_sealed_receipt_replay(
    tmp_path, monkeypatch, drift
):
    snap, packet, ref, root, initial, *_ = setup(tmp_path / "case")
    # A synthetic known-value DATA read port only. Redaction matcher/policy,
    # projection, role validation, actual receipt reader and raw guards stay real.
    synthetic_values = []
    monkeypatch.setattr(credential_redaction, "_known_values", lambda: list(synthetic_values))
    if drift:
        synthetic_values.append(
            next(row["content"].encode() for row in initial["catalog"] if row["kind"] == "junit")
        )
    view = semantic.catalog_input(snap, packet, ref, root)
    output = answer(view)
    if drift:
        # No proof claim from a redacted view; the receipt only records advice.
        for row in output["obligations"]:
            row.update(status="unknown", gap="evidence_inconclusive", citations=[])
    state = sealed_input(tmp_path / "receipt", monkeypatch, snap, ref, view, output)
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snap))
    prep = {
        "snapshot": snap,
        "snapshot_ref": {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        },
        "revision": snap["revision"],
        "packet_ref": ref,
    }
    if drift:
        synthetic_values.clear()  # Would reveal formerly hidden bytes on rebuild.
        assert semantic.catalog_digest(
            semantic.catalog_input(snap, packet, ref, root)
        ) != semantic.catalog_digest(view)
        with pytest.raises(RuntimeError):
            actual_replay(root)(state, snap["target"], prep)
    else:
        rebuilt = actual_replay(root)(state, snap["target"], prep)["proposal"]
        assert rebuilt["semantic_assessment"] == output
        assert state["actual"]["_closer_controlled_proof"]["expected"]["basis"][
            "semantic_catalog_sha256"
        ] == semantic.catalog_digest(view)
        assert rebuilt["authority"] is False
