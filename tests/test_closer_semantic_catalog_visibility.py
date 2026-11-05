"""Actual collector/admitted XML index; model quotes only its bounded view."""

import hashlib
import io
import zipfile

import pytest

import closer_disposition as disposition
import closer_evidence as evidence
import closer_semantic_assessment as semantic


@pytest.fixture(autouse=True)
def synthetic_redaction_port(monkeypatch):
    # Shape policy stays active; synthetic test input never reads a live file.
    monkeypatch.setenv("ORCH_REDACT_SECRET_FILES", "")


def collected(tmp_path, *, count=1, redacted=False, filler_bytes=18000):
    snap = {
        "target": "catalog/test#11",
        "kind": "pr",
        "head": "a" * 40,
        "base": "b" * 40,
        "revision": "r1",
        "review_revision": "reviews1",
        "checks": "SUCCESS",
        "threads": [],
        "sources": [
            {
                "target": "catalog/test#2",
                "body": "## Tasks\n- Inspect the supplied report receipt.",
                "state": "open",
                "labels": [],
            }
        ],
    }
    sensitive = " sk-" + "A" * 30 if redacted else ""
    raw = (
        "<testsuite><system-out>"
        + sensitive
        + "F" * filler_bytes
        + '</system-out><testcase name="LATE_SYNTHETIC_ONLY"/></testsuite>'
    ).encode()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for n in range(count):
            archive.writestr(f"{n:02}.xml", raw)

    def api(endpoint, **_):
        if "/compare/" in endpoint:
            return {"status": "ahead", "files": []}
        if "/actions/runs?" in endpoint:
            return {"workflow_runs": [{"id": 7, "head_sha": snap["head"]}]}
        if endpoint.endswith("/runs/7"):
            return {
                "id": 7,
                "run_attempt": 1,
                "head_sha": snap["head"],
                "status": "completed",
                "conclusion": "success",
                "pull_requests": [{"number": 11}],
            }
        if endpoint.endswith("/runs/7/artifacts?per_page=100"):
            return {
                "artifacts": [
                    {
                        "id": 9,
                        "name": "synthetic-xml",
                        "size_in_bytes": len(stream.getvalue()),
                        "expired": False,
                    }
                ]
            }
        raise AssertionError(endpoint)

    root = tmp_path / "evidence"
    packet = evidence.collect_evidence(
        snap, api, root, artifact_fetch=lambda *_a, **_k: stream.getvalue()
    )
    path = root / "manifests" / (packet["manifest"]["packet_sha256"] + ".json")
    ref = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    built = semantic._build(snap, packet, ref, root)

    def check(number, excerpt):
        value = {
            "schema": semantic.SCHEMA,
            "obligations": [
                {
                    "slot": row["slot"],
                    "meaning": "understood",
                    "status": "unknown",
                    "gap": "evidence_inconclusive",
                    "explanation": "The supplied partial receipt leaves this original synthetic question unresolved.",
                    "citations": [
                        {
                            "ref": number,
                            "excerpt": excerpt,
                            "relevance": "This visible literal source excerpt is the limited evidence for the named original question.",
                        }
                    ],
                }
                for row in built[-1]["obligations"]
            ],
            "findings": [],
            "global_concerns": [],
        }
        return semantic.materialize(
            value, snapshot=snap, packet=packet, packet_ref=ref, retained_dir=root
        )

    return raw, built, check, packet, root


def test_full_validated_xml_and_stable_index_project_only_actual_prefix(tmp_path):
    raw, built, check, packet, root = collected(tmp_path)
    view = built[-1]
    row = view["catalog"][0]
    assert row["content"].encode() == raw[:16384]
    visibility = row["visibility"]
    assert visibility["total_bytes"] == len(raw)
    assert visibility["supplied_bytes"] == visibility["raw_prefix_bytes"] == 16384
    assert visibility["truncated"] and visibility["citable"]
    assert not visibility["redaction_applied"]
    assert "LATE_SYNTHETIC_ONLY" not in row["content"]
    assert check(row["ref"], "<testsuite>")["semantic_readiness"] == "knowledge_incomplete"
    entry = built[-2][row["ref"] - 1]
    proof = {
        **entry["member"],
        "evidence_sha256": entry["ref"]["sha256"],
        "excerpt": "LATE_SYNTHETIC_ONLY",
        "relevance": "This authentic private tail is deliberately not visible in the supplied view.",
    }
    assert disposition._proofs_valid(
        [entry["ref"]], [proof], disposition._receipt_index(packet, root), root
    )
    with pytest.raises(ValueError, match="outside_supplied_visible"):
        check(row["ref"], "LATE_SYNTHETIC_ONLY")


def test_omitted_entries_keep_numbers_and_cannot_be_guessed_from_retained_raw(tmp_path):
    raw, built, check, packet, root = collected(tmp_path, count=9)
    entries, view = built[-2], built[-1]
    assert len(entries) == len(view["catalog"]) == 9
    assert [row["ref"] for row in view["catalog"]] == list(range(1, 10))
    omitted = next(
        row for row in view["catalog"] if row.get("visibility", {}).get("content_supplied") is False
    )
    assert omitted["content"] == "" and omitted["visibility"]["citable"] is False
    entry = entries[omitted["ref"] - 1]
    assert disposition._safe_junit_raw(entry["ref"], root) == raw
    with pytest.raises(ValueError, match="outside_supplied_visible"):
        check(omitted["ref"], "<testsuite>")
    assert (
        sum(
            row.get("visibility", {}).get("supplied_bytes", len(row["content"].encode()))
            for row in view["catalog"]
        )
        <= 32768
    )


def test_redacted_context_is_explicit_and_never_citable(tmp_path):
    _raw, built, check, _packet, _root = collected(tmp_path, redacted=True)
    row = built[-1]["catalog"][0]
    assert row["visibility"]["redaction_applied"]
    assert row["visibility"]["citable"] is False
    assert "sk-" + "A" * 30 not in row["content"]
    with pytest.raises(ValueError, match="outside_supplied_visible"):
        check(row["ref"], "<testsuite>")
