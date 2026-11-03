"""Necessary durable handoff containment; no genuine issuer or Git permission."""

import ast
from pathlib import Path

import pytest
from test_closer_coding_publication import custody as custody
from test_closer_publication_mutation_body import inputs
from test_closer_publication_private_staging import build

import closer_coding_publication as pub
import closer_correction_controller as ctl


def handoff_data(custody):
    prepared, _, _ = inputs()
    key = prepared["intent"]["slot_key"]
    reference = {
        "schema": "closer-publication-source-baseline-reference/v1",
        "name": "baseline-data",
        "sha256": "a" * 64,
    }
    terminal = {"schema": "coding-terminal-data", "name": "terminal-data", "sha256": "b" * 64}
    record = {
        "schema": "closer-publication-source-handoff/v3",
        "baseline_reference": reference,
        "staging_link_name": "publication-staging-link-" + key + ".json",
        "candidate": prepared["intent"]["candidate"],
        "terminal_reference": terminal,
        "result_sha256": "c" * 64,
        "operation_sha256": "d" * 64,
        "stable_slot": key,
        **{k: False for k in pub._AUTHORITY},
    }
    name = "publication-source-handoff-" + pub.digest(record) + ".json"
    assert custody.create(name, record)
    return record, {
        "schema": "closer-publication-source-handoff-reference/v1",
        "name": name,
        "sha256": pub.digest(record),
    }


def detached_finish():
    """Only actual post-issuance try/return, with the genuine issuer still held."""
    tree = ast.parse(Path(pub.__file__).read_text())
    function = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "retain_handoff"
    )
    tail = function.body[-3:]
    assert (
        isinstance(tail[0], ast.Assign)
        and isinstance(tail[1], ast.Try)
        and isinstance(tail[2], ast.Return)
    )
    wrapper = ast.FunctionDef(
        name="finish",
        args=ast.arguments(
            posonlyargs=[],
            args=[
                ast.arg(arg=x)
                for x in (
                    "custody",
                    "baseline_ref",
                    "candidate",
                    "terminal_ref",
                    "handoff_reference",
                )
            ],
            kwonlyargs=[],
            kw_defaults=[],
            defaults=[],
        ),
        body=tail,
        decorator_list=[],
    )
    namespace = dict(pub.__dict__)
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])),
            "<detached-known-handoff-containment>",
            "exec",
        ),
        namespace,
    )
    return namespace["finish"]


def test_actual_source_stores_handoff_before_staging():
    tree = ast.parse(Path(pub.__file__).read_text())
    function = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "retain_handoff"
    )
    create = next(
        n
        for n in ast.walk(function)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "create"
    )
    prepare = next(
        n
        for n in ast.walk(function)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_issue_private_staging"
    )
    assert create.lineno < prepare.lineno
    ref = next(
        n
        for n in function.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "handoff_reference" for t in n.targets)
    )
    assert create.lineno < ref.lineno < prepare.lineno


def test_real_unissued_staging_preserves_exact_known_reference(custody):
    record, reference = handoff_data(custody)
    before = (custody.path / reference["name"]).read_bytes()
    result = detached_finish()(
        custody, None, record["candidate"], record["terminal_reference"], reference
    )
    assert result is reference
    assert (custody.path / reference["name"]).read_bytes() == before
    assert not (custody.path / record["staging_link_name"]).exists()
    held = custody.read("publication-staging-hold-" + reference["sha256"] + ".json")
    assert held["code"] == "staging_preparation_held"
    assert all(held[k] is False for k in pub._AUTHORITY)
    # Even a duplicate diagnostic refusal cannot erase the issued reference.
    assert (
        detached_finish()(
            custody, None, record["candidate"], record["terminal_reference"], reference
        )
        is reference
    )


def test_partial_local_preparation_does_not_erase_handoff(custody, monkeypatch):
    record, reference = handoff_data(custody)

    def fail_write(*args):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(pub.os, "write", fail_write)
    with pytest.raises(OSError):
        build(custody, record["stable_slot"])
    assert (custody.path / ("publication-staging-" + record["stable_slot"])).exists()
    with pytest.raises(pub.PublicationHeld, match="already owned"):
        build(custody, record["stable_slot"])
    assert (
        detached_finish()(
            custody, None, record["candidate"], record["terminal_reference"], reference
        )
        is reference
    )


def linked_data(custody):
    handoff, reference = handoff_data(custody)
    staging = {
        "schema": "closer-publication-source-staging/v2",
        "slot_key": handoff["stable_slot"],
        "handoff_reference": reference,
        "baseline_reference": handoff["baseline_reference"],
        "candidate_sha256": pub.digest(handoff["candidate"]),
        "terminal_reference": handoff["terminal_reference"],
        "operation_sha256": handoff["operation_sha256"],
        "admission_sha256": "a" * 64,
        "publication_policy_sha256": "b" * 64,
        "private_files": {"DATA": True},
        **{k: False for k in pub._AUTHORITY},
    }
    name = "publication-source-staging-" + pub.digest(staging) + ".json"
    assert custody.create(name, staging)
    stage_ref = {
        "schema": "closer-publication-source-staging-reference/v1",
        "name": name,
        "sha256": pub.digest(staging),
    }
    return handoff, reference, stage_ref


def test_link_is_separate_create_only_and_does_not_change_handoff(custody):
    handoff, reference, stage_ref = linked_data(custody)
    original = (custody.path / reference["name"]).read_bytes()
    link = pub._link_staging_data(custody, reference, stage_ref)
    assert link["name"] == handoff["staging_link_name"]
    assert (custody.path / reference["name"]).read_bytes() == original
    with pytest.raises(pub.PublicationHeld, match="already owned"):
        pub._link_staging_data(custody, reference, stage_ref)


def test_link_readback_failure_preserves_owned_record_and_known_handoff(custody, monkeypatch):
    handoff, reference, stage_ref = linked_data(custody)
    read = ctl._Custody.read

    def fail_link_read(self, name):
        if name == handoff["staging_link_name"]:
            raise OSError("synthetic readback failure")
        return read(self, name)

    monkeypatch.setattr(ctl._Custody, "read", fail_link_read)
    with pytest.raises(OSError):
        pub._link_staging_data(custody, reference, stage_ref)
    assert (custody.path / handoff["staging_link_name"]).exists()
    assert (
        detached_finish()(
            custody, None, handoff["candidate"], handoff["terminal_reference"], reference
        )
        is reference
    )
    monkeypatch.setattr(ctl._Custody, "read", read)
    with pytest.raises(pub.PublicationHeld, match="already owned"):
        pub._link_staging_data(custody, reference, stage_ref)


def test_older_frozen_handoff_shape_is_not_reinterpreted(custody):
    handoff, reference = handoff_data(custody)
    handoff["schema"] = "closer-publication-source-handoff/v2"
    name = "publication-source-handoff-" + pub.digest(handoff) + ".json"
    assert custody.create(name, handoff)
    old = {**reference, "name": name, "sha256": pub.digest(handoff)}
    with pytest.raises(pub.PublicationHeld, match="immutable known handoff differs"):
        pub._read_handoff_data(custody, old)


@pytest.mark.parametrize("fatal", [KeyboardInterrupt, SystemExit, GeneratorExit])
@pytest.mark.parametrize("stage", ["preparation", "diagnostic"])
def test_fatal_refusal_propagates_after_durable_known_handoff(custody, fatal, stage):
    handoff, reference = handoff_data(custody)
    before = (custody.path / reference["name"]).read_bytes()
    finish = detached_finish()

    def refuse(*args):
        raise fatal()

    # Refusal-only port in the detached tail namespace, never a successful
    # source validator/issuer or a production-module replacement.
    finish.__globals__[
        "_issue_private_staging" if stage == "preparation" else "_retain_staging_hold_data"
    ] = refuse
    with pytest.raises(fatal):
        finish(custody, None, handoff["candidate"], handoff["terminal_reference"], reference)
    assert (custody.path / reference["name"]).read_bytes() == before
    assert not (custody.path / handoff["staging_link_name"]).exists()
