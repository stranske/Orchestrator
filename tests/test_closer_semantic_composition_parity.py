"""Explicit prospective v2 delta against unchanged historical optional API data."""

import copy
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest
from test_closer_semantic_assessment import answer, setup
from test_closer_semantic_optional_accounting_scope import optional_row

import closer_semantic_assessment as composed

API = (
    Path(__file__).parent / "fixtures" / "semantic_api_reference" / "optional_accounting_api.py.txt"
)


@pytest.mark.parametrize(
    "mode",
    ["ordinary", "required_unknown", "nonpass_omitted", "nonpass_optional", "optional_meaning"],
)
def test_prospective_v2_diff_from_frozen_api_is_exact_and_source_owned(tmp_path, mode):
    loader = SourceFileLoader("frozen_optional_api", str(API))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    frozen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(frozen)
    native = mode.startswith("nonpass") or mode == "optional_meaning"
    snap, packet, ref, root, view, *_ = setup(tmp_path, native=native)
    old_view = frozen.catalog_input(snap, packet, ref, root)
    assert view == {**old_view, "host_residual_holds": view["host_residual_holds"]}
    assert composed.catalog_digest(view) != frozen.catalog_digest(old_view)
    value = answer(view)
    if mode == "required_unknown":
        value["obligations"][0].update(status="unknown", gap="evidence_inconclusive")
    elif mode in {"nonpass_optional", "optional_meaning"}:
        value["obligations"] = [optional_row(view, "evidence_inconclusive")]
        if mode == "optional_meaning":
            value["obligations"][0].update(meaning="unknown", gap="obligation_ambiguous")
    args = dict(
        snapshot=snap,
        packet=packet,
        packet_ref=ref,
        retained_dir=root,
        expected_catalog_sha256=composed.catalog_digest(view),
    )
    legacy_value = {**copy.deepcopy(value), "schema": frozen.SCHEMA}
    legacy_args = {**args, "expected_catalog_sha256": frozen.catalog_digest(old_view)}
    expected = frozen.materialize(legacy_value, **legacy_args)
    expected["semantic_assessment"] = copy.deepcopy(value)
    expected["host_residual_holds"] = view["host_residual_holds"]
    if view["host_residual_holds"]:
        expected["classification"] = "uncertain"
        expected["semantic_readiness"] = "held"
        expected["substantive_needs"] = list(
            dict.fromkeys(
                [
                    *expected["substantive_needs"],
                    *("host_OTHER:" + hold for hold in view["host_residual_holds"]),
                ]
            )
        )
    assert composed.materialize(copy.deepcopy(value), **args) == expected


def test_api_shared_functions_and_optional_unknown_carrier_have_exact_ast():
    import ast

    frozen_tree = ast.parse(API.read_text())
    composed_tree = ast.parse(Path(composed.__file__).read_text())
    frozen_functions = {
        node.name: node for node in frozen_tree.body if isinstance(node, ast.FunctionDef)
    }
    composed_functions = {
        node.name: node for node in composed_tree.body if isinstance(node, ast.FunctionDef)
    }

    def dump(node):
        return ast.dump(node, include_attributes=False)

    for name, node in frozen_functions.items():
        if name not in {"materialize", "_build"}:
            assert dump(node) == dump(composed_functions[name]), name

    class RemoveHostInput(ast.NodeTransformer):
        count = 0

        def visit_Dict(self, node):
            node = self.generic_visit(node)
            for i, key in enumerate(node.keys):
                if isinstance(key, ast.Constant) and key.value == "host_residual_holds":
                    value = node.values[i]
                    assert isinstance(value, ast.Call) and value.func.id == "host_residual_holds"
                    self.count += 1
                    del node.keys[i]
                    del node.values[i]
                    break
            return node

    restore = RemoveHostInput()
    normalized = restore.visit(copy.deepcopy(composed_functions["_build"]))
    assert restore.count == 1
    assert dump(normalized) == dump(frozen_functions["_build"])

    # The materializer intentionally factors state into _assessment_state for
    # selected Strong reconciliation. Its API result/carrier correction itself
    # is exact executable AST, and the five controls above compare full outputs.
    def carrier(fn):
        return next(
            node
            for node in ast.walk(fn)
            if isinstance(node, ast.Dict)
            and any(isinstance(key, ast.Constant) and key.value == "criterion" for key in node.keys)
        )

    assert dump(carrier(frozen_functions["materialize"])) == dump(
        carrier(composed_functions["materialize"])
    )
