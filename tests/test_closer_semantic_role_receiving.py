"""Current source intended-use category, not model labels or acceptance authority."""

import ast
import copy
from pathlib import Path

import pytest
from test_closer_semantic_assessment import setup

import closer_dispatcher as closer


def test_typed_ordinary_evidence_advice_allocates_source_category(tmp_path):
    snapshot, _packet, _ref, _root, view, *_ = setup(tmp_path)
    assert (
        closer._typed_ordinary_static_category(snapshot, view, collection_mode=False)
        == "ordinary_static"
    )
    # Category is an advisory use, not a semantic assertion about criterion proof.
    changed = copy.deepcopy(view)
    changed["obligations"][0]["text"] = "Run the explicitly required integration command."
    assert (
        closer._typed_ordinary_static_category(snapshot, changed, collection_mode=False)
        == "ordinary_static"
    )


@pytest.mark.parametrize(
    "damage", ["collection", "known_nonpass", "unknown_mode", "residual", "security", "authority"]
)
def test_neighbor_protected_and_nonordinary_uses_get_no_category(tmp_path, damage):
    snapshot, _packet, _ref, _root, view, *_ = setup(tmp_path)
    if damage == "known_nonpass":
        view["mode"] = "known_NONPASS_accounting"
    elif damage == "unknown_mode":
        view["mode"] = "unknown"
    elif damage == "residual":
        view["host_residual_holds"] = ["native_ci_failed_without_same_boundary_failed_receipt"]
    elif damage == "security":
        snapshot["risk"] = {"category": "security"}
    elif damage == "authority":
        snapshot["risk"] = {"authority_conflict": True}
    assert (
        closer._typed_ordinary_static_category(
            snapshot, view, collection_mode=damage == "collection"
        )
        is None
    )


def test_actual_source_builds_authenticated_catalog_before_category_and_keeps_it_on_retry():
    tree = ast.parse(Path(closer.__file__).read_text())
    pipeline = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_run_staged_pipeline"
    )
    interpret = next(
        n for n in pipeline.body if isinstance(n, ast.FunctionDef) and n.name == "interpret"
    )
    calls = [n for n in ast.walk(interpret) if isinstance(n, ast.Call)]
    catalog = next(
        n for n in calls if isinstance(n.func, ast.Attribute) and n.func.attr == "catalog_input"
    )
    category = next(
        n
        for n in calls
        if isinstance(n.func, ast.Name) and n.func.id == "_typed_ordinary_static_category"
    )
    routes = [
        n for n in calls if isinstance(n.func, ast.Name) and n.func.id == "_pipeline_low_route"
    ]
    assert len(routes) == 2 and catalog.lineno < category.lineno < min(n.lineno for n in routes)
    for call in routes:
        keyword = next(k for k in call.keywords if k.arg == "closer_category")
        assert isinstance(keyword.value, ast.Name) and keyword.value.id == "closer_category"
    execute = next(
        n for n in calls if isinstance(n.func, ast.Name) and n.func.id == "_execute_profile_route"
    )
    assert any(
        k.arg == "closer_category" and isinstance(k.value, ast.Name) for k in execute.keywords
    )
    # Source-level collection returns before catalog/category/LOW selection.
    collection = next(
        n
        for n in ast.walk(interpret)
        if isinstance(n, ast.If)
        and isinstance(n.test, ast.Name)
        and n.test.id == "collection_mode"
        and any(
            isinstance(x, ast.Import)
            and any(a.name == "closer_semantic_assessment" for a in x.names)
            for x in n.body
        )
    )
    assert collection.end_lineno < catalog.lineno
    assert any(isinstance(n, ast.Return) for n in collection.body)
