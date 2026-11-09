"""Exact prospective optional-audit delta; no original/source guard is relaxed."""

import ast
import copy
import hashlib

from closer_semantic_audit_support import fingerprint, functions, source_path
from closer_semantic_native_integration_support import (
    advisory_plan_omission_reference,
    before_finite_prepare_source,
)


def assignment(node, name):
    return isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == name for target in node.targets
    )


def inner(run, name):
    return next(
        node for node in run.body if isinstance(node, ast.FunctionDef) and node.name == name
    )


def reviewed_pipeline_before_plan_omission(source):
    """Test-only exact prospective projection before the historical audit."""
    source = before_finite_prepare_source(source)
    pin = advisory_plan_omission_reference()["dispatcher"]
    assert hashlib.sha256(source.encode()).hexdigest() == pin["after_file_sha256"]
    run = copy.deepcopy(functions(source)["_run_staged_pipeline"])
    prepare = inner(run, "prepare")
    assert fingerprint(prepare) == pin["after_prepare_ast_sha256"]
    position = next(
        i for i, node in enumerate(prepare.body) if assignment(node, "semantic_cache_key")
    )
    assert not any(assignment(node, "drift") for node in prepare.body)
    prepare.body.insert(
        position,
        ast.parse(
            "drift = closer_evidence.drift_sample(packet, recurring=True) if closer_evidence.validate_packet(packet) else []"
        ).body[0],
    )
    result = next(node for node in prepare.body if isinstance(node, ast.Return))
    summary = next(
        value
        for key, value in zip(result.value.keys, result.value.values)
        if isinstance(key, ast.Constant) and key.value == "evidence_summary"
    )
    assert not any(
        isinstance(key, ast.Constant) and key.value == "drift_sample" for key in summary.keys
    )
    position = next(
        i
        for i, key in enumerate(summary.keys)
        if isinstance(key, ast.Constant) and key.value == "sections"
    )
    summary.keys.insert(position, ast.Constant(value="drift_sample"))
    summary.values.insert(position, ast.Name(id="drift", ctx=ast.Load()))
    assert fingerprint(prepare) == pin["before_prepare_ast_sha256"]
    return run


def test_unsupported_optional_audit_cannot_allocate_a_model_job_or_invalidate_evidence_cache():
    run = functions(source_path("closer_dispatcher.py").read_text())["_run_staged_pipeline"]
    interpret = inner(run, "interpret")
    values = [node.value for node in ast.walk(interpret) if assignment(node, "run_drift")]
    assert values and all(
        isinstance(value, ast.Constant) and value.value is False for value in values
    )
    prepare = inner(run, "prepare")
    # The original per-PR evidence key is reused; there is no time-based audit
    # generation whose expiry could buy another assessment on unchanged proof.
    assigned = [node for node in ast.walk(prepare) if assignment(node, "semantic_cache_key")]
    assert len(assigned) == 1
    assert ast.unparse(assigned[0].value) == "packet.get('semantic_cache_key')"
    assert not any(assignment(node, "audit_generation") for node in run.body)
