"""Synthetic native data controls; actual four-ZIP replay is retained separately."""

import copy
import hashlib
import io
import json
import time
import zipfile
from pathlib import Path

import pytest
from test_closer_disposition import fixture

import closer_correction_bridge as bridge
import closer_correction_classification as contract
import closer_evidence as evidence


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def pack(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as z:
        for name, raw in files.items():
            z.writestr(name, raw)
    return stream.getvalue()


@pytest.fixture
def native_world(tmp_path):
    snap, packet, ref, _rootprep, _low = fixture(tmp_path)
    root = Path(ref["path"]).parent.parent
    source = snap["sources"][0]
    source.update(
        kind="issue", node_id="I_2", provenance={"body_sha256": sha(source["body"].encode())}
    )
    snap.update(state="MERGED", merge="d" * 40)
    request = {
        "schema": "verification-request/v1",
        "sources": [
            {
                "repository": "owner/repo",
                "kind": "issue",
                "number": "2",
                "body_sha256": sha(source["body"].encode()),
            }
        ],
    }
    material = {
        "schema": "verification-material/v1",
        "code": {"head_sha": snap["head"], "evaluated_sha": snap["merge"]},
    }
    event = {
        "schema": "verification-event/v1",
        "repo": "owner/repo",
        "pr": "12",
        "head_sha": snap["head"],
        "evaluated_sha": snap["merge"],
        "request_sha256": contract.digest(request),
        "material_sha256": contract.digest(material),
    }
    event["event_id"] = contract.digest(event)
    assessment = {
        "mode": "compare",
        "provider": "",
        "model": "synthetic",
        "model2": "",
        "canonical": True,
    }
    manifest = {
        "verification_event": event,
        "request": request,
        "material": material,
        "assessment_identity": assessment,
    }
    snap["verifier"] = {
        "schema": "verifier-corpus-decision/v1",
        "verdict": "NON_PASS",
        "repo": "owner/repo",
        "pr": 12,
        "head_sha": snap["head"],
        "evaluated_sha": snap["merge"],
        "run_id": 34,
        "run_attempt": 1,
        "provider_verdicts": ["FAIL"],
        "ci_failed": False,
        "verification_event": event,
    }
    snap["verifier_origin"] = {
        "schema": "closer-terminal-comment-origin/v1",
        "comment_id": "77",
        "comment_sha256": "a" * 64,
        "decision_sha256": "b" * 64,
        "author": "github-actions[bot]",
        "actor_type": "Bot",
    }
    report = {
        "results": [
            {
                "used_llm": True,
                "verdict": "FAIL",
                "error": None,
                "concerns": ["The required report is absent."],
            }
        ],
        "report": "Synthetic retained final data.",
    }
    raw_report = evidence._bytes(report)
    raw_evidence = {
        "ref": "comparison.json#/results/0/concerns/0",
        "sha256": sha(report["results"][0]["concerns"][0].encode()),
    }
    seed = {
        "schema": "closer-correction-seed/v1",
        "target": {
            "repository": "owner/repo",
            "pr": "12",
            "head_sha": snap["head"],
            "base_sha": snap["base"],
            "merge_sha": snap["merge"],
        },
        "source_issue": {
            "number": "2",
            "node_id": "I_2",
            "body_sha256": sha(source["body"].encode()),
        },
        "original_verifier": {
            "verification_event": event,
            "run_id": "34",
            "run_attempt": "1",
            **{
                k: snap["verifier_origin"][k]
                for k in ("comment_id", "comment_sha256", "decision_sha256")
            },
        },
        "finding": {"id": "raw-" + contract.digest(raw_evidence), "evidence": raw_evidence},
        "scope": "substantive_implementation",
    }
    bundle = {
        "schema": "closer-correction-seed-bundle/v1",
        "verification_event": event,
        "assessment_identity": assessment,
        "result_sha256": sha(raw_report),
        "seeds": [seed],
    }
    bundle["bundle_sha256"] = contract.digest(bundle)
    files = {
        "comparison.json": raw_report,
        "closer-correction-seeds.json": evidence._bytes(bundle),
        "verification-event-manifest.json": evidence._bytes(manifest),
        "verifier-context.md": b"Synthetic original source body",
        "verifier-pr-diff.patch": b"+def ship_report(): pass",
        "verifier-diff-summary.md": b"One source change",
    }
    capture = {
        "schema": "workflows-verifier-input-snapshot/v1",
        "capture_kind": "production",
        "mode": "compare",
        "repository": "owner/repo",
        "pr": 12,
        "merge_sha": snap["merge"],
        "source_run_id": "34",
    }
    for name, key in [
        ("verifier-context.md", "context_sha256"),
        ("verifier-pr-diff.patch", "diff_sha256"),
        ("verifier-diff-summary.md", "diff_summary_sha256"),
    ]:
        capture[key] = sha(files[name])
    files["verifier-input-manifest.json"] = evidence._bytes(capture)
    run = {
        "id": 34,
        "run_attempt": 1,
        "event": "workflow_dispatch",
        "path": ".github/workflows/agents-verifier.yml",
        "status": "completed",
        "repository": {"full_name": "owner/repo"},
    }
    inventory = [
        {"id": 35, "name": "comparison-results-34", "expired": False},
        {"id": 36, "name": "verifier-terminal-disposition-34", "expired": True},
        {"id": 37, "name": "agents-verifier-metrics", "unknown": True},
        {"id": 38, "name": "verifier-closure-plans-34"},
    ]
    return {
        "snapshot": snap,
        "packet": packet,
        "root": root,
        "files": files,
        "run": run,
        "inventory": inventory,
    }


def collect(w, *, total=None, raw=None):
    downloads = []

    def api(endpoint):
        if endpoint.endswith("/artifacts?per_page=100"):
            return {
                "artifacts": copy.deepcopy(w["inventory"]),
                "total_count": len(w["inventory"]) if total is None else total,
            }
        return copy.deepcopy(w["run"])

    def fetch(artifact, **_):
        downloads.append(artifact["id"])
        assert artifact["id"] == 35, "auxiliary metadata is never requested as input proof"
        return raw if raw is not None else pack(w["files"])

    result = evidence._native_verifier_section(
        w["snapshot"], api, time.monotonic() + 10, w["root"], fetch
    )
    return result, downloads


def joined(w, section):
    packet = copy.deepcopy(w["packet"])
    packet["bindings"]["source"] = evidence._hash(w["snapshot"]["sources"])
    packet["sections"]["source"] = evidence._source_section(w["snapshot"]["sources"], w["root"])
    packet["sections"]["native_merge_verifier"] = section
    for value in packet["sections"].values():
        value.pop("retained", None)
        value["sha256"] = evidence._section_hash(value)
        value["retained"] = evidence._retain_bytes(w["root"], evidence._bytes(value), "json")
    packet["section_digests"] = {
        k: evidence._section_hash(v) for k, v in packet["sections"].items()
    }
    packet["manifest"] = {
        "section_files": {k: v["retained"] for k, v in packet["sections"].items()}
    }
    packet["manifest"]["packet_sha256"] = evidence._packet_hash(packet)
    path = w["root"] / "manifests" / (packet["manifest"]["packet_sha256"] + ".json")
    path.write_bytes(evidence._bytes(packet))
    ref = {"path": str(path), "sha256": sha(path.read_bytes())}
    return bridge.collect_original_inputs(w["snapshot"], packet, ref)


def test_auxiliary_inventory_is_retained_without_capture_or_proof(native_world):
    w = native_world
    section, downloads = collect(w)
    assert section["status"] == "complete" and downloads == [35]
    assert len(section["content"]["auxiliary_artifacts"]) == 3
    assert all(
        x["capture_requested"] is False and x["proof_status"] == "not-native-input-proof"
        for x in section["content"]["auxiliary_artifacts"]
    )
    result = joined(w, section)
    assert len(result["raw_findings"]) == len(result["native_seed_mapping"]) == 1
    assert result["remote_authenticated"] is False
    # The snapshot lacks request/material by design; only its distinct event receipt has them.
    assert "verification_event" not in section["content"]["input_manifest"]
    assert any(
        r["kind"] == "native-verification-event-manifest"
        for r in section["content"]["artifacts"][0]["receipts"]
    )


@pytest.mark.parametrize(
    "damage",
    [
        "missing_capture",
        "duplicate_capture",
        "partial_inventory",
        "foreign_run",
        "wrong_source",
        "corrupt_zip",
        "crc",
        "duplicate_event_json",
        "required_context",
        "missing_event",
        "duplicate_event",
        "traversal_event",
        "wrong_event",
        "wrong_request",
        "wrong_material",
        "aux_event_only",
    ],
)
def test_missing_ambiguous_or_corrupt_required_capture_remains_held(native_world, damage):
    w = native_world
    raw, total = None, None
    if damage == "missing_capture":
        w["inventory"] = w["inventory"][1:]
    elif damage == "duplicate_capture":
        w["inventory"].append({"id": 39, "name": "evaluation-results-34"})
    elif damage == "partial_inventory":
        total = 9
    elif damage == "foreign_run":
        w["run"]["id"] = 99
    elif damage == "wrong_source":
        value = json.loads(w["files"]["verifier-input-manifest.json"])
        value["source_run_id"] = "99"
        w["files"]["verifier-input-manifest.json"] = evidence._bytes(value)
    elif damage == "corrupt_zip":
        raw = b"not a ZIP"
    elif damage == "crc":
        raw = pack(w["files"]).replace(
            b"Synthetic original source body", b"Xynthetic original source body", 1
        )
    elif damage == "duplicate_event_json":
        w["files"]["verification-event-manifest.json"] = (
            w["files"]["verification-event-manifest.json"][:-1] + b',"request":{}}'
        )
    elif damage == "required_context":
        w["files"].pop("verifier-context.md")
    elif damage in {"missing_event", "aux_event_only"}:
        if damage == "aux_event_only":
            # Correct-looking metadata outside the selected canonical archive
            # cannot substitute for that archive's exact retained event member.
            w["inventory"][1]["verification_event_manifest"] = json.loads(
                w["files"]["verification-event-manifest.json"]
            )
        w["files"].pop("verification-event-manifest.json")
    elif damage == "duplicate_event":
        w["files"]["nested/verification-event-manifest.json"] = w["files"][
            "verification-event-manifest.json"
        ]
    elif damage == "traversal_event":
        w["files"]["../verification-event-manifest.json"] = w["files"].pop(
            "verification-event-manifest.json"
        )
    else:
        value = json.loads(w["files"]["verification-event-manifest.json"])
        if damage == "wrong_event":
            value["verification_event"]["event_id"] = "0" * 64
        elif damage == "wrong_request":
            value["request"]["sources"] = []
        elif damage == "wrong_material":
            value["material"]["code"]["head_sha"] = "0" * 40
        w["files"]["verification-event-manifest.json"] = evidence._bytes(value)
    section, _ = collect(w, total=total, raw=raw)
    with pytest.raises((ValueError, KeyError)):
        joined(w, section)
