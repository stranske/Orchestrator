"""Portable historical source audits; fingerprints are data, never authority."""

import ast
import copy
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).parent
REFERENCE = ROOT / "fixtures" / "semantic_api_reference"
MANIFEST_SHA256 = "b6d3690ef4284ae144d3b9cb5ac96022d7997fbebe5beb5e47cd559b36b579e7"


def stable_ast(node):
    # Match the existing portable fixture audit normalization: parser-only empty
    # fields vary across supported Python minors. Retain every node, nonempty
    # field and list position, including constants and executable control flow.
    if isinstance(node, ast.AST):
        return {
            "type": type(node).__name__,
            "fields": {
                key: stable_ast(value)
                for key, value in ast.iter_fields(node)
                if value is not None and value != []
            },
        }
    if isinstance(node, list):
        return [stable_ast(value) for value in node]
    if isinstance(node, bytes):
        return {"literal_type": "bytes", "hex": node.hex()}
    if node is Ellipsis:
        return {"literal_type": "ellipsis"}
    if isinstance(node, complex):
        return {"literal_type": "complex", "real": node.real, "imag": node.imag}
    return node


def fingerprint(node):
    return hashlib.sha256(
        json.dumps(stable_ast(node), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def functions(source):
    return {
        node.name: node
        for node in ast.parse(source).body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef))
    }


def leaves(source):
    return {name: fingerprint(node) for name, node in functions(source).items()}


def unreserved_pipeline(source):
    run = copy.deepcopy(functions(source)["_run_staged_pipeline"])
    run.body = [
        node
        for node in run.body
        if not (
            isinstance(node, ast.FunctionDef)
            and node.name in {"interpret", "full_interpretation", "exception", "action_backend"}
        )
    ]
    return fingerprint(run)


def reference(base):
    raw = (REFERENCE / "historical_guard_audits.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == MANIFEST_SHA256
    return json.loads(raw)["baselines"][base]


def source_path(filename):
    source_root = ROOT.parent / "src"
    if not source_root.is_dir():
        source_root = ROOT.parent  # Existing flat exec-mirror layout.
    return source_root / filename


def source_unchanged(base, filename):
    expected = reference(base)["files"][filename]["sha256"]
    return hashlib.sha256(source_path(filename).read_bytes()).hexdigest() == expected


def original_act_merge():
    expected = reference("9379f225")["act_merge"]
    raw = (REFERENCE / expected["filename"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == expected["sha256"]
    tree = ast.parse(raw)
    assert len(tree.body) == 1 and isinstance(tree.body[0], ast.Assign)
    assert fingerprint(tree.body[0]) == expected["ast_sha256"]
    return tree.body[0]
