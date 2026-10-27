"""Pure portability/provenance controls; no fixture construction or subprocess."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import closer_semantic_fixture_support as support

TESTS = Path(__file__).parent
REFERENCE = TESTS / "fixtures" / "semantic_api_reference" / "optional_accounting_api.py.txt"
REFERENCE_SHA256 = "bf0b96f7cf0d0655d021d5e72a09df2e36798616cc110e16ce676dea1c74cf68"
REVIEWED_HELPER_AST_SHA256 = {
    "blob": "83a93758373cdc1cbc6b43d14befdf46e633b071981d311ca62fbec2e22d335e",
    "physical_product": "b05b52e9dfb78a843e163b10cf060c32d45bc5c1dfc3a826e08a0107dba54ac7",
    "closed_function": "102e12e6982af37539a2c15ae4ea59911ab92964c2e58c37fbe97417f1420d31",
}


def _stable_ast(node):
    # Empty parser metadata such as Python 3.12+'s type_params does not change
    # these reviewed function semantics. Keep nonempty fields and list positions.
    if isinstance(node, ast.AST):
        return {
            "type": type(node).__name__,
            "fields": {
                key: _stable_ast(value)
                for key, value in ast.iter_fields(node)
                if value is not None and value != []
            },
        }
    if isinstance(node, list):
        return [_stable_ast(value) for value in node]
    return node


def test_optional_api_reference_is_exact_historical_test_snapshot():
    assert hashlib.sha256(REFERENCE.read_bytes()).hexdigest() == REFERENCE_SHA256


def test_minimal_helpers_keep_reviewed_executable_ast():
    tree = ast.parse(Path(support.__file__).read_text())
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert set(functions) == set(REVIEWED_HELPER_AST_SHA256)
    for name, expected in REVIEWED_HELPER_AST_SHA256.items():
        actual = hashlib.sha256(
            json.dumps(_stable_ast(functions[name]), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        assert actual == expected, name


def test_portable_semantic_definitions_do_not_import_from_machine_scratch_paths():
    files = [
        TESTS / "test_closer_semantic_composition_parity.py",
        TESTS / "semantic_composition_preexec" / "test_original_mechanisms.py",
        Path(support.__file__),
        REFERENCE,
    ]
    for path in files:
        tree = ast.parse(path.read_text())
        assert not any(
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith(("/tmp/", "/private/tmp/"))
            for node in ast.walk(tree)
        ), path.name
        assert not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Attribute)
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "sys"
            and node.func.value.attr == "path"
            and node.func.attr in {"insert", "append", "extend"}
            for node in ast.walk(tree)
        ), path.name
