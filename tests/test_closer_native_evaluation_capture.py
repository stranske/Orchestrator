"""Single-provider native input recovery stays diagnostic and exactly bound."""

import hashlib
import io
import json
import time
import zipfile
from pathlib import Path

import pytest
from test_closer_verifier_attribution import MERGE, evaluation_archive
from test_closer_verifier_attribution import world as world

import closer_evidence as evidence
import closer_verifier_boundary as boundary


def replace_file(data, name, raw):
    with zipfile.ZipFile(io.BytesIO(data)) as source:
        files = {key: source.read(key) for key in source.namelist()}
    if raw is None:
        files.pop(name)
    else:
        files[name] = raw
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as target:
        for key, content in files.items():
            target.writestr(key, content)
    return output.getvalue()


def test_single_evaluation_retains_one_provider_and_actual_untrusted_diagnostics(tmp_path):
    rows = evidence._native_manifest_from_archive(evaluation_archive(), tmp_path)
    assert len(rows) == 1 and not rows[0]["omissions"]
    receipts = rows[0]["receipts"]
    report = next(r for r in receipts if r["kind"] == "native-evaluation")
    assert not any(r["kind"] == "native-comparison" for r in receipts)
    assert report["results_count"] == 1 and len(report["provider_results"]) == 1
    assert report["provider_results"][0]["provider_used"] == "anthropic/example"
    assert report["provider_results"][0]["used_llm"] is True
    assert [r["text"] for r in report["findings"]] == ["One retained diagnostic concern"]
    assert report["acceptance_authority"] is report["resolution_authority"] is False
    assert report["text_is_untrusted"] is True
    for kind in ["native-evaluation-comment", "native-evaluation-execution"]:
        receipt = next(r for r in receipts if r["kind"] == kind)
        assert receipt["acceptance_authority"] is receipt["resolution_authority"] is False
        assert receipt["text_is_untrusted"] is True
        raw = Path(receipt["raw"]["path"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == receipt["raw"]["sha256"]


@pytest.mark.parametrize("raw", [b"{", b"[]"])
def test_malformed_evaluation_is_named_omission_not_invented_model_result(tmp_path, raw):
    data = replace_file(evaluation_archive(), "evaluation.json", raw)
    row = evidence._native_manifest_from_archive(data, tmp_path)[0]
    assert any(r["path"] == "evaluation.json" for r in row["omissions"])
    assert not any(r.get("provider_results") for r in row["receipts"])


def test_evaluation_transport_failure_does_not_invent_successful_judge(tmp_path):
    raw = json.dumps(
        {
            "used_llm": False,
            "error": "startup unavailable",
            "concerns": [],
            "summary": "No model decision",
            "verdict": "UNVERIFIABLE_INPUT",
        }
    ).encode()
    row = evidence._native_manifest_from_archive(
        replace_file(evaluation_archive(), "evaluation.json", raw), tmp_path
    )[0]
    report = next(r for r in row["receipts"] if r["kind"] == "native-evaluation")
    assert len(report["provider_results"]) == 1
    provider = report["provider_results"][0]
    assert provider["used_llm"] is False and provider["error"] == "startup unavailable"
    assert report["acceptance_authority"] is False and not report["findings"]


def test_missing_evaluation_report_is_named_without_losing_manifest(tmp_path):
    row = evidence._native_manifest_from_archive(
        replace_file(evaluation_archive(), "evaluation.json", None), tmp_path
    )[0]
    assert row["manifest"]["pr"] == 867
    assert {"path": "evaluation.json", "reason": "native evaluation report missing"} in row[
        "omissions"
    ]
    assert not any(r.get("provider_results") for r in row["receipts"])


def test_dual_report_json_is_ambiguous_without_losing_input_identity(tmp_path):
    data = replace_file(evaluation_archive(), "comparison.json", b'{"results":[]}')
    row = evidence._native_manifest_from_archive(data, tmp_path)[0]
    assert row["manifest"]["pr"] == 867
    assert any(r["reason"] == "native reports ambiguous" for r in row["omissions"])
    assert not any(r.get("provider_results") for r in row["receipts"])
    assert not any(
        r.get("kind") in {"native-comparison", "native-evaluation"} for r in row["receipts"]
    )


@pytest.mark.parametrize("pr", [12, 867])
def test_integrated_large_evaluation_identity_and_diagnostics(world, tmp_path, pr):
    """Merged transport bounds and evaluation-family reader compose without PASS."""
    world.capture_family = "evaluation"
    data = evaluation_archive(pr, MERGE if pr == 12 else "e" * 40)
    context = b"authentic original request\n" + b"x" * 471507
    data = replace_file(data, "verifier-context.md", context)
    with zipfile.ZipFile(io.BytesIO(data)) as source:
        manifest = json.loads(source.read("verifier-input-manifest.json"))
    manifest["context_sha256"] = hashlib.sha256(context).hexdigest()
    manifest["discovery_inventory"] = "x" * 409289
    world.data = replace_file(data, "verifier-input-manifest.json", json.dumps(manifest).encode())
    relation, _ = boundary.attribute(
        world.run,
        world.snapshot,
        world.backend.gh.api,
        lambda *_a, **_k: world.data,
        world.backend.verifier_archive_root,
        deadline=time.monotonic() + 30,
        reads=[],
    )
    assert relation == ("same" if pr == 12 else "foreign")
    assert not world.posts and world.snapshot.get("verifier") is None
    if pr != 12:
        return
    ordinary = world.backend.verification_snapshot(world.snapshot)
    assert ordinary.get("native_diagnostic_reference") is None
    missing = evidence._native_verifier_section(
        ordinary,
        world.backend.gh.api,
        time.monotonic() + 30,
        tmp_path / "ordinary-packet",
        lambda *_a, **_k: world.data,
        native_archive_root=world.backend.verifier_archive_root,
    )
    assert missing["status"] == "missing"
    assert missing["reason"] == "no trusted exact-identity NON_PASS marker for current merged PR"
    # Historical diagnostic capture is a targeted opt-in, never initial admission.
    observed = world.backend.verification_snapshot(world.snapshot, include_history=True)
    assert observed["native_diagnostic_reference"]["subject_pr"] == "owner/repo#12"
    assert observed.get("verifier") is None
    section = evidence._native_verifier_section(
        observed,
        world.backend.gh.api,
        time.monotonic() + 30,
        tmp_path / "packet",
        lambda *_a, **_k: world.data,
        native_archive_root=world.backend.verifier_archive_root,
    )
    assert section["status"] == "unknown"
    assert section["content"]["acceptance_authority"] is False
    artifact = section["content"]["artifacts"][0]
    captured = artifact["input_snapshots"][0]
    assert captured["raw"]["bytes"] > evidence.MAX_SECTION_BYTES
    assert captured["manifest_projection"]["acceptance_authority"] is False
    assert "discovery_inventory" not in captured["manifest"]
    report = next(r for r in captured["receipts"] if r["kind"] == "native-evaluation")
    assert report["results_count"] == 1
    assert len(report["provider_results"]) == 1
    assert report["acceptance_authority"] is report["resolution_authority"] is False
    assert not world.posts
