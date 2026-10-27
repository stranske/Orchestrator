"""Pure native/semantic source-union guards; no original process mechanism run."""

import ast
import hashlib

import closer_semantic_native_integration_support as support
import pytest
from closer_semantic_audit_support import fingerprint, functions, source_path


def test_native_operational_and_latest_reader_sources_are_exact_reviewed0953():
    for filename, expected in support.current_receiver_reference()[
        "protected_native_source_sha256"
    ].items():
        assert hashlib.sha256(source_path(filename).read_bytes()).hexdigest() == expected, filename


def test_current_native_top_level_entry_functions_are_not_backrevved():
    top = functions(source_path("closer_dispatcher.py").read_text())
    for name, expected in support.current_receiver_reference()["native_top_level_ast"].items():
        assert fingerprint(top[name]) == expected, name


def test_native_retained_packet_custody_guard_survives_semantic_interpret_composition():
    run = functions(source_path("closer_dispatcher.py").read_text())["_run_staged_pipeline"]
    interpret = next(
        node for node in run.body if isinstance(node, ast.FunctionDef) and node.name == "interpret"
    )
    block = next(
        node
        for node in ast.walk(interpret)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "data_only"
        and any(
            isinstance(row, ast.Assign)
            and isinstance(row.value, ast.Call)
            and isinstance(row.value.func, ast.Name)
            and row.value.func.id == "data_only_evidence_context"
            for row in node.body
        )
    )
    assert (
        fingerprint(ast.Module(body=block.body[:2], type_ignores=[]))
        == support.native_reference()["native_custody_guard_ast"]
    )
    call = next(
        row.value
        for row in block.body
        if isinstance(row, ast.Assign)
        and isinstance(row.value, ast.Call)
        and isinstance(row.value.func, ast.Name)
        and row.value.func.id == "data_only_evidence_context"
    )
    selected = next(value.value for value in call.keywords if value.arg == "retained_dir")
    assert isinstance(selected, ast.Name)
    assert selected.id == support.native_reference()["retained_dir_name"] == "ci_retained_root"


def test_native_integration_manifest_is_pinned_not_a_mutable_allowlist(tmp_path, monkeypatch):
    path = tmp_path / "bad.json"
    path.write_bytes(support.MANIFEST.read_bytes().replace(b"0953", b"0000", 1))
    monkeypatch.setattr(support, "MANIFEST", path)
    with pytest.raises(AssertionError):
        support.native_reference()
