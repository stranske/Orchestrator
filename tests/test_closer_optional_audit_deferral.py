"""Exact prospective optional-audit delta; no original/source guard is relaxed."""

import ast
import copy
import hashlib

import pytest
from closer_semantic_audit_support import fingerprint, functions, source_path
from closer_semantic_native_integration_support import (
    advisory_plan_omission_reference,
    audit_deferral_reference,
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


def test_only_optional_audit_and_cache_job_changes_restore_exact_reviewed_a715_pipeline():
    reference = audit_deferral_reference()
    source = source_path("closer_dispatcher.py").read_text()
    run = reviewed_pipeline_before_plan_omission(source)
    restore = reference["restore_only"]
    position = next(i for i, node in enumerate(run.body) if assignment(node, "drift_candidate"))
    run.body.insert(position + 1, ast.parse(restore["generation"]).body[0])
    prepare = inner(run, "prepare")
    position = next(i for i, node in enumerate(prepare.body) if assignment(node, "audit_due"))
    prepare.body.insert(position + 1, ast.parse(restore["cache_if"]).body[0])
    interpret = inner(run, "interpret")
    position = next(
        i
        for i, node in enumerate(interpret.body)
        if isinstance(node, ast.Nonlocal) and node.names == ["drift_claimed"]
    )
    assert assignment(interpret.body[position + 1], "audit_unavailable_current_contract")
    interpret.body[position : position + 2] = ast.parse(restore["selection_with"]).body
    audit = next(node for node in interpret.body if assignment(node, "producer_audit"))
    status = next(i for i, key in enumerate(audit.value.keys) if key.value == "status")
    audit.value.values[status] = ast.parse(restore["producer_status"], mode="eval").body
    assert fingerprint(run) == reference["before_pipeline_ast_sha256"]
    # No arbitrary source edit may be normalized into the historical baseline.
    for changed in (
        source + "\n# unreviewed source change\n",
        source.replace("run_drift = False", "run_drift = True", 1),
        source.replace('"packet_ref": packet_ref', '"packet_ref": None', 1),
    ):
        assert changed != source
        with pytest.raises(AssertionError):
            reviewed_pipeline_before_plan_omission(changed)


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
