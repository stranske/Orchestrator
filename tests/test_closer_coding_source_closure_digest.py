"""Detached digest-domain controls; no source installation or admission issued."""

import ast
import hashlib
from pathlib import Path

import pytest

import closer_correction_controller as ctl
import closer_correction_handoff as handoff
import closer_source_installation as source


def closure(count):
    return {f"module_{i:03d}.py": hashlib.sha256(str(i).encode()).hexdigest() for i in range(count)}


def actual_comparison(value, record):
    """Evaluate only the actual closed DATA comparison, without its authority frame."""
    tree = ast.parse(Path(source.__file__).read_text())
    function = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_coding_snapshot_membership"
    )
    comparison = next(
        n
        for n in ast.walk(function)
        if isinstance(n, ast.Compare)
        and isinstance(n.left, ast.Subscript)
        and isinstance(n.left.slice, ast.Constant)
        and n.left.slice.value == "source_closure_sha256"
    )
    return eval(
        compile(ast.Expression(comparison), "<actual-source-closure-comparison>", "eval"),
        {"ctl": ctl, "_digest": source._digest},
        {"value": {"source_closure": value}, "record": record},
    )


@pytest.mark.parametrize("count", [64, 65, 165])
def test_internal_closure_matches_actual_issuer_domain(count):
    value = closure(count)
    assert actual_comparison(value, {"source_closure_sha256": ctl.digest(value)})


@pytest.mark.parametrize("count", [65, 165])
def test_public_handoff_grammar_still_refuses_large_objects(count):
    with pytest.raises(ValueError, match="JSON object exceeds bound"):
        handoff.digest(closure(count))


def test_public_boundary_and_internal_bytes_agree_at_64():
    value = closure(64)
    assert handoff.MAX_LIST == 64
    assert handoff.digest(value) == ctl.digest(value)


@pytest.mark.parametrize("count", [64, 65, 165])
def test_wrong_protected_closure_hash_remains_false(count):
    assert not actual_comparison(closure(count), {"source_closure_sha256": "0" * 64})


@pytest.mark.parametrize(
    "value",
    [{"module.py": 1.5}, {"module.py": 2**53}, {1: "bad-key"}, {"module.py": "x" * 1048576}],
)
def test_internal_private_json_bounds_remain_enforced(value):
    with pytest.raises((ValueError, ctl.ControllerUnknown, UnicodeError)):
        actual_comparison(value, {"source_closure_sha256": "0" * 64})


def test_internal_depth_bound_remains_enforced():
    value = "leaf"
    for _ in range(34):
        value = {"child": value}
    with pytest.raises((ValueError, ctl.ControllerUnknown), match="structure exceeds bound"):
        actual_comparison(value, {"source_closure_sha256": "0" * 64})


def test_changed_source_member_changes_issuer_digest():
    value = closure(165)
    original = ctl.digest(value)
    value["module_000.py"] = "f" * 64
    assert not actual_comparison(value, {"source_closure_sha256": original})
