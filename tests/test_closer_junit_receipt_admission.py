"""Offline typed-receipt admission; no decision or acceptance is constructed."""

from __future__ import annotations

import copy
import io
import json
import zipfile
from pathlib import Path

import pytest
from test_closer_disposition import fixture

import closer_disposition as disposition
import closer_evidence as evidence


def junit_bytes(size=300_000):
    return (
        b'<testsuite tests="1"><testcase name="Ship the report"><system-out>'
        + b"x" * size
        + b"</system-out></testcase></testsuite>"
    )


def receipt_fixture(tmp_path, *, partial=True, raw=None):
    _, packet, packet_ref, _, _ = fixture(tmp_path)
    root = Path(packet_ref["path"]).parent.parent
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("runtimes/3.12/pytest-junit.xml", raw or junit_bytes())
        if partial:
            archive.writestr("index.ndjson", '{"index":"ancillary"}\n')
            archive.writestr("runtimes/3.12/coverage.xml", "<coverage/>")
            archive.writestr("runtimes/3.12/pytest-collection.json", '{"collection":[]}')
    receipts, omissions, status = evidence._parse_archive(stream.getvalue(), root)
    assert len(receipts) == 1
    artifact = packet["sections"]["ci_artifacts"]["content"]["artifacts"][0]
    artifact.update(
        status=status,
        receipts=receipts,
        omissions=omissions,
        archive=evidence._retain_bytes(root, stream.getvalue(), "zip"),
    )
    section = packet["sections"]["ci_artifacts"]
    # Unknown topology stays unknown: a named raw receipt is not global CI proof.
    section.update(status="truncated", truncated=True, reason="required topology unknown")
    return packet, root, artifact, receipts[0]


def key(receipt):
    return receipt["raw"]["path"], receipt["raw"]["sha256"]


@pytest.mark.parametrize("partial", [False, True])
def test_complete_large_junit_is_indexed_and_consumed_without_promoting_ci(tmp_path, partial):
    packet, root, artifact, receipt = receipt_fixture(tmp_path, partial=partial)
    original = copy.deepcopy(packet)
    raw = receipt["raw"]
    assert raw["bytes"] > evidence.MAX_SECTION_BYTES
    assert disposition._safe_raw(raw, root) is None  # General cap remains256KB.
    assert disposition._safe_junit_raw(raw, root) == junit_bytes()
    index = disposition._receipt_index(packet, root)
    assert key(receipt) in index
    member = index[key(receipt)]
    assert member == {
        "artifact_id": artifact["id"],
        "run_id": artifact["run_id"],
        "run_attempt": artifact["run_attempt"],
        "head_sha": packet["head"],
        "kind": "junit",
        "receipt_path": receipt["path"],
    }
    proof = {
        **member,
        "evidence_sha256": raw["sha256"],
        "excerpt": '<testcase name="Ship the report">',
        "relevance": "The named passed testcase exercises shipping the report.",
    }
    assert disposition._proofs_valid([raw], [proof], index, root)
    assert not disposition._proofs_valid(
        [raw], [{**proof, "run_attempt": member["run_attempt"] + 1}], index, root
    )
    assert not disposition._proofs_valid(
        [raw], [{**proof, "excerpt": "not present in retained XML"}], index, root
    )
    assert packet == original  # No artifact, section, or global completeness promotion.


@pytest.mark.parametrize(
    "omissions",
    [
        [],
        [{"reason": "unknown omission"}],
        [{"reason": "ZIP entry count or uncompressed size cap exceeded"}],
        [{"reason": "unsafe or directory entry omitted"}],
        [{"reason": "invalid or unsafe XML"}],
        [{"reason": "entry size mismatch"}],
        [
            {"reason": "file format is not a recognized test or coverage receipt"},
            {"reason": "invalid JSON"},
        ],
    ],
)
def test_partial_junit_needs_only_known_ancillary_omissions(tmp_path, omissions):
    packet, root, artifact, receipt = receipt_fixture(tmp_path)
    artifact["omissions"] = omissions
    assert key(receipt) not in disposition._receipt_index(packet, root)


@pytest.mark.parametrize(
    "change",
    [
        "tests",
        "failures",
        "errors",
        "skipped",
        "artifact_head",
        "run_head",
        "artifact_attempt",
        "run_status",
        "run_conclusion",
        "truncated_artifact",
    ],
)
def test_junit_reparsed_counts_and_run_provenance_must_match(tmp_path, change):
    packet, root, artifact, receipt = receipt_fixture(tmp_path)
    run = next(
        row
        for row in packet["sections"]["ci_artifacts"]["content"]["runs"]
        if row["id"] == artifact["run_id"]
    )
    if change in {"tests", "failures", "errors", "skipped"}:
        receipt[change] += 1
    elif change == "artifact_head":
        artifact["head_sha"] = "c" * 40
    elif change == "run_head":
        run["head_sha"] = "c" * 40
    elif change == "artifact_attempt":
        artifact["run_attempt"] += 1
    elif change == "run_status":
        run["status"] = "in_progress"
    elif change == "run_conclusion":
        run["conclusion"] = "failure"
    else:
        artifact["status"] = "truncated"
    assert key(receipt) not in disposition._receipt_index(packet, root)


@pytest.mark.parametrize(
    "raw",
    [
        b'<testsuite><testcase name="Ship the report"></testsuite>',
        b'<!DOCTYPE testsuite [<!ENTITY name "Ship the report">]>'
        b'<testsuite><testcase name="&name;"/></testsuite>',
        b'{"tests":1,"passed":1,"failed":0}',
    ],
)
def test_typed_junit_reader_rejects_bad_xml_entities_and_nonxml(tmp_path, raw):
    root = tmp_path / "retained"
    ref = evidence._retain_bytes(root, raw, "xml")
    assert disposition._safe_junit_raw(ref, root) is None


@pytest.mark.parametrize(
    "tamper", ["hash", "bytes", "escape", "symlink", "raw_root_symlink", "oversize"]
)
def test_junit_typed_reader_retains_file_safety_and_byte_bound(tmp_path, tamper):
    root = tmp_path / "retained"
    ref = evidence._retain_bytes(root, junit_bytes(), "xml")
    if tamper == "hash":
        ref["sha256"] = "0" * 64
    elif tamper == "bytes":
        ref["bytes"] += 1
    elif tamper == "escape":
        outside = tmp_path / "outside.xml"
        outside.write_bytes(junit_bytes())
        ref["path"] = str(outside)
    elif tamper == "symlink":
        link = root / "raw" / "linked.xml"
        link.symlink_to(ref["path"])
        ref["path"] = str(link)
    elif tamper == "raw_root_symlink":
        old = root / "raw"
        moved = root / "moved"
        old.rename(moved)
        old.symlink_to(moved, target_is_directory=True)
    else:
        ref = evidence._retain_bytes(root, junit_bytes(evidence.MAX_UNCOMPRESSED_BYTES + 1), "xml")
    assert disposition._safe_junit_raw(ref, root) is None


@pytest.mark.parametrize("kind", ["test-summary", "coverage-summary"])
def test_partial_other_receipt_kinds_do_not_gain_junit_exception(tmp_path, kind):
    packet, root, artifact, _ = receipt_fixture(tmp_path)
    doc = {"total": 1, "passed": 1, "failed": 0, "skipped": 0}
    row = {"kind": kind, **doc}
    if kind == "coverage-summary":
        row = {"kind": kind, "count": 1, "covered": 1, "percent_covered": 100}
    row.update(
        path="summary.json", raw=evidence._retain_bytes(root, json.dumps(doc).encode(), "json")
    )
    artifact["receipts"] = [row]
    assert key(row) not in disposition._receipt_index(packet, root)


def test_non_junit_proof_keeps_original_raw_cap(tmp_path):
    root = tmp_path / "retained"
    ref = evidence._retain_bytes(root, junit_bytes(), "xml")
    assert disposition._safe_raw(ref, root) is None
    member = {"kind": "codepatch", "filename": "report.py"}
    index = {(ref["path"], ref["sha256"]): member}
    proof = {
        **member,
        "evidence_sha256": ref["sha256"],
        "excerpt": "Ship the report",
        "relevance": "No typed JUnit authority for patches.",
    }
    assert not disposition._proofs_valid([ref], [proof], index, root)
