"""Pure guards for compact immutable audit data; no historical Git invocation."""

import ast
import copy
import hashlib
from pathlib import Path

import closer_semantic_audit_support as audit
import pytest


def test_manifest_is_pinned_and_original_act_merge_is_only_one_assignment():
    for base in ("78f6244c", "9379f225"):
        record = audit.reference(base)
        assert record["commit"].startswith(base)
        assert all(len(row["git_blob"]) == 40 for row in record["files"].values())
    node = audit.original_act_merge()
    assert isinstance(node, ast.Assign)
    assert len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
    assert node.targets[0].id == "merged"
    assert isinstance(node.value, ast.Call)
    assert node.value.func.attr == "merge_selected_response"


@pytest.mark.parametrize(
    "before,after",
    [
        ("value = 1", "value = 2"),
        ("value = True", "value = 1"),
        ("value = b'proof'", "value = 'proof'"),
        ("value = [1, 2]", "value = [2, 1]"),
        ("if valid: grant()", "if not valid: grant()"),
        ("def f(): return allowed()", "def f(): return refused()"),
    ],
)
def test_fingerprint_keeps_executable_values_types_order_and_guards(before, after):
    assert audit.fingerprint(ast.parse(before)) != audit.fingerprint(ast.parse(after))


def test_whitespace_does_not_change_ast_audit():
    assert audit.fingerprint(ast.parse("value=1")) == audit.fingerprint(ast.parse("value = 1\n"))


def test_manifest_tamper_fails_before_baseline_use(tmp_path, monkeypatch):
    raw = (audit.REFERENCE / "historical_guard_audits.json").read_bytes()
    changed = raw.replace(b"78f6244c", b"78f6244d", 1)
    assert hashlib.sha256(changed).hexdigest() != audit.MANIFEST_SHA256
    (tmp_path / "historical_guard_audits.json").write_bytes(changed)
    monkeypatch.setattr(audit, "REFERENCE", tmp_path)
    with pytest.raises(AssertionError):
        audit.reference("78f6244c")


def test_original_assignment_tamper_fails_before_restoration(tmp_path, monkeypatch):
    (tmp_path / "historical_guard_audits.json").write_bytes(
        (audit.REFERENCE / "historical_guard_audits.json").read_bytes()
    )
    (tmp_path / "legacy_act_merge.py.txt").write_text("merged = None\n")
    monkeypatch.setattr(audit, "REFERENCE", tmp_path)
    with pytest.raises(AssertionError):
        audit.original_act_merge()


def test_unreserved_pipeline_mutation_remains_detected():
    source = audit.source_path("closer_dispatcher.py").read_text()
    run = copy.deepcopy(audit.functions(source)["_run_staged_pipeline"])
    expected = audit.unreserved_pipeline(source)
    # A statement outside the four explicitly reserved semantic functions cannot
    # disappear behind the prospective role-contract exclusions.
    run.body.append(ast.Pass())
    changed = ast.unparse(ast.Module(body=[run], type_ignores=[]))
    assert audit.unreserved_pipeline(changed) != expected


def test_audited_tests_do_not_invoke_historical_git_or_external_scratch_imports():
    for name in (
        "test_closer_semantic_assessment.py",
        "test_closer_semantic_proof_union.py",
        "test_closer_semantic_dispatcher_glue.py",
        "closer_semantic_audit_support.py",
    ):
        tree = ast.parse((Path(__file__).parent / name).read_text())
        for node in ast.walk(tree):
            assert not (
                isinstance(node, ast.Import)
                and any(alias.name == "subprocess" for alias in node.names)
            ), name
            assert not (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.startswith(("/tmp/", "/private/tmp/"))
            ), name


@pytest.mark.parametrize("layout", ["checkout", "flat"])
def test_source_byte_guard_preserves_checkout_and_flat_layout(tmp_path, monkeypatch, layout):
    source = audit.source_path("closer_disposition.py").read_bytes()
    tests = tmp_path / "tests"
    tests.mkdir()
    destination = tmp_path / "src" if layout == "checkout" else tmp_path
    destination.mkdir(exist_ok=True)
    path = destination / "closer_disposition.py"
    path.write_bytes(source)
    monkeypatch.setattr(audit, "ROOT", tests)
    assert audit.source_unchanged("9379f225", "closer_disposition.py")
    path.write_bytes(source + b"\n# changed source binding\n")
    assert not audit.source_unchanged("9379f225", "closer_disposition.py")
