"""Sealed transport identity limits are distinct from model presentation budgets."""

import hashlib
import io
import json
import time
import zipfile

import pytest
from test_closer_evidence import base_api, snapshot
from test_closer_verifier_attribution import HEAD, MERGE, TARGET, archive
from test_closer_verifier_attribution import world as world

import closer_evidence as evidence
import closer_verifier_boundary as boundary


def capture(*, pr=858, merge="e" * 40, padding=409289, context_size=471507, corrupt=False):
    with zipfile.ZipFile(io.BytesIO(archive(pr=pr, merge=merge))) as z:
        files = {n: z.read(n) for n in z.namelist()}
    files["verifier-context.md"] = b"authored original request\n" + b"x" * (context_size - 26)
    manifest = json.loads(files["verifier-input-manifest.json"])
    manifest["context_sha256"] = (
        "0" * 64 if corrupt else hashlib.sha256(files["verifier-context.md"]).hexdigest()
    )
    manifest["discovery_inventory"] = ""
    manifest["discovery_inventory"] = "x" * max(0, padding - len(json.dumps(manifest).encode()))
    files["verifier-input-manifest.json"] = json.dumps(manifest).encode()
    return pack(files)


def pack(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return stream.getvalue()


def attribute(world, tmp_path):
    reads = []
    result = boundary.attribute(
        world.run,
        world.snapshot,
        world.backend.gh.api,
        lambda *a, **k: world.data,
        tmp_path,
        deadline=time.monotonic() + 30,
        reads=reads,
    )
    return result, reads


def test_large_bound_native_input_remains_foreign_not_unattributed(world, tmp_path):
    world.data = capture()
    result, reads = attribute(world, tmp_path)
    assert result == ("foreign", None)
    assert any(r["endpoint"].startswith("archive:") for r in reads)
    assert not world.posts


def test_large_native_transport_reaches_compact_valid_packet(world, tmp_path):
    world.data = capture(pr=12, merge=MERGE)
    reference = {
        "subject_pr": TARGET,
        "head": HEAD,
        "merge": MERGE,
        "run": world.run,
        "archive_digest": hashlib.sha256(world.data).hexdigest(),
    }
    snap = snapshot(
        target=TARGET, head=HEAD, merge=MERGE, state="MERGED", native_diagnostic_reference=reference
    )

    def api(endpoint, **kwargs):
        return (
            base_api(endpoint, **kwargs)
            if "/compare/" in endpoint
            else world.backend.gh.api(endpoint, **kwargs)
        )

    packet = evidence.collect_evidence(
        snap, api, tmp_path, artifact_fetch=lambda *a, **k: world.data
    )
    section = packet["sections"]["native_merge_verifier"]
    assert section["status"] != "truncated"
    assert section["content"]["artifacts"][0]["identity_bound"] is True
    captured = section["content"]["artifacts"][0]["input_snapshots"][0]
    assert captured["manifest"]["pr"] == 12
    assert "discovery_inventory" not in captured["manifest"]
    assert captured["manifest_projection"]["full_content_bytes"] > evidence.MAX_SECTION_BYTES
    assert captured["raw"]["bytes"] > evidence.MAX_SECTION_BYTES
    assert section["content"]["acceptance_authority"] is False
    assert evidence.validate_packet(packet)
    assert not world.posts


@pytest.mark.parametrize("component", ["verifier-input-manifest.json", "verifier-context.md"])
def test_required_native_transport_limit_is_named_recoverable(
    world, tmp_path, monkeypatch, component
):
    world.data = capture(context_size=100) if component.endswith(".json") else capture(padding=1000)
    monkeypatch.setattr(evidence, "MAX_NATIVE_INPUT_BYTES", 300000)
    result, _ = attribute(world, tmp_path)
    assert result[0] == "pending"
    assert "native input transport limit" in result[1]
    assert component in result[1]
    assert not world.posts


@pytest.mark.parametrize(
    "change",
    [
        "hash",
        "duplicate_manifest",
        "duplicate_context",
        "traversal_context",
        "duplicate_key",
        "attempt",
        "repo",
        "run",
        "same_pr_wrong_merge",
    ],
)
def test_large_capture_identity_and_ambiguity_guards_remain(world, tmp_path, change):
    world.data = capture(pr=12, merge=MERGE)
    with zipfile.ZipFile(io.BytesIO(world.data)) as z:
        files = {n: z.read(n) for n in z.namelist()}
    doc = json.loads(files["verifier-input-manifest.json"])
    if change == "hash":
        files["verifier-context.md"] += b"changed"
    elif change == "duplicate_manifest":
        files["nested/verifier-input-manifest.json"] = files["verifier-input-manifest.json"]
    elif change == "duplicate_context":
        files["nested/verifier-context.md"] = files["verifier-context.md"]
    elif change == "traversal_context":
        files["../verifier-context.md"] = files["verifier-context.md"]
    elif change == "duplicate_key":
        files["verifier-input-manifest.json"] = (
            files["verifier-input-manifest.json"][:-1] + b', "pr": 12}'
        )
    else:
        key, val = {
            "attempt": ("source_run_attempt", 2),
            "repo": ("repository", "foreign/repo"),
            "run": ("source_run_id", "100"),
            "same_pr_wrong_merge": ("merge_sha", "e" * 40),
        }[change]
        doc[key] = val
        files["verifier-input-manifest.json"] = json.dumps(doc).encode()
    world.data = pack(files)
    result, _ = attribute(world, tmp_path)
    assert result[0] in {"pending", "mismatch"}
    assert not world.posts


@pytest.mark.parametrize("which", ["compressed", "expanded", "count"])
def test_archive_global_bounds_remain_named(world, tmp_path, monkeypatch, which):
    world.data = capture()
    name, limit = {
        "compressed": ("MAX_ARCHIVE_BYTES", len(world.data) - 1),
        "expanded": ("MAX_UNCOMPRESSED_BYTES", 100000),
        "count": ("MAX_FILES", 1),
    }[which]
    monkeypatch.setattr(evidence, name, limit)
    with pytest.raises(evidence.NativeInputCaptureError, match="native input transport limit"):
        evidence._native_manifest_from_archive(world.data, tmp_path)


def test_unknown_oversize_entry_not_admitted_as_native_input(world, tmp_path):
    with zipfile.ZipFile(io.BytesIO(capture())) as z:
        files = {n: z.read(n) for n in z.namelist()}
    files["unknown.json"] = b"x" * (evidence.MAX_SECTION_BYTES + 1)
    found = evidence._native_manifest_from_archive(pack(files), tmp_path)
    assert len(found) == 1
    assert all(receipt.get("path") != "unknown.json" for receipt in found[0]["receipts"])


def test_actual_stream_bytes_are_bounded_independently_of_metadata(world, tmp_path, monkeypatch):
    world.data = capture(context_size=100, padding=1000)
    original = zipfile.ZipFile.open

    def inflated(self, member, *args, **kwargs):
        if getattr(member, "filename", member) == "verifier-context.md":
            return io.BytesIO(b"x" * 2001)
        return original(self, member, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", inflated)
    monkeypatch.setattr(evidence, "MAX_NATIVE_INPUT_BYTES", 2000)
    with pytest.raises(evidence.NativeInputCaptureError, match="limit or size mismatch"):
        evidence._native_manifest_from_archive(world.data, tmp_path)


@pytest.mark.parametrize("component", ["verifier-input-manifest.json", "verifier-context.md"])
@pytest.mark.parametrize("prefix", ["../", "/"])
def test_single_unsafe_required_path_has_named_recoverable_failure(
    world, tmp_path, component, prefix
):
    with zipfile.ZipFile(io.BytesIO(capture())) as z:
        files = {name: z.read(name) for name in z.namelist()}
    files[prefix + component] = files.pop(component)
    world.data = pack(files)
    result, _ = attribute(world, tmp_path)
    assert result[0] == "pending"
    assert "native input canonical path invalid: " + component in result[1]
    assert not world.posts


def large_nonpass_fixture(tmp_path, monkeypatch):
    from test_closer_disposition import fixture

    collect = evidence.collect_evidence

    def collect_large(*args, artifact_fetch, **kwargs):
        def fetch(artifact, **fetch_kwargs):
            raw = artifact_fetch(artifact, **fetch_kwargs)
            if artifact.get("id") != 35:
                return raw
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                files = {name: z.read(name) for name in z.namelist()}
            manifest = json.loads(files.pop("input.json"))
            files["verifier-context.md"] = b"authored request\n" + b"x" * 471507
            files["verifier-diff-summary.md"] = b"report.py added"
            for name, key in (
                ("verifier-context.md", "context_sha256"),
                ("verifier-pr-diff.patch", "diff_sha256"),
                ("verifier-diff-summary.md", "diff_summary_sha256"),
            ):
                manifest[key] = hashlib.sha256(files[name]).hexdigest()
            manifest["discovery_inventory"] = "x" * 409289
            files["verifier-input-manifest.json"] = json.dumps(manifest).encode()
            return pack(files)

        return collect(*args, artifact_fetch=fetch, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(evidence, "collect_evidence", collect_large)
        return fixture(tmp_path, native=True)


def test_large_native_nonpass_reaches_resolution_without_acceptance(tmp_path, monkeypatch):
    from pathlib import Path

    from test_closer_disposition import HEAD, check

    import closer_disposition as disposition

    fx = large_nonpass_fixture(tmp_path, monkeypatch)
    snap, packet, ref, _prep, interpretation = fx
    content = packet["sections"]["native_merge_verifier"]["content"]
    assert content["input_manifest"]["raw"]["bytes"] > evidence.MAX_SECTION_BYTES
    assert content["input_manifest_projection"]["acceptance_authority"] is False
    raw = content["artifacts"][0]["receipts"][1]["raw"]
    index = disposition._native_resolution_index(
        packet, Path(ref["path"]).parent.parent, snap["verifier"]
    )
    assert (raw["path"], raw["sha256"]) in index
    pending = disposition.pending_nonpass_findings(snap, packet)
    assert any(row["raw"]["source"] == "native_provider_concern" for row in pending)
    interpretation["proposal"]["resolutions"] = [
        {
            "finding_id": row["finding_id"],
            "status": "resolved_nonissue",
            "next_guarded_action": "wait",
            "evidence": [raw],
            "proofs": [
                {
                    "evidence_sha256": raw["sha256"],
                    "kind": "native-comparison",
                    "artifact_id": 35,
                    "run_id": 34,
                    "run_attempt": 1,
                    "evaluated_sha": snap["merge"],
                    "head_sha": HEAD,
                    "receipt_path": "comparison.json",
                    "excerpt": "Ship the report",
                    "relevance": "The native comparison finding concerns this report.",
                }
            ],
        }
        for row in pending
    ]
    result = check(fx)
    assert result["status"] == "ordinary_nonpass", result
    assert result["disposition"]["acceptance_authority"] is False
    assert any(
        row["raw"]["source"] == "native_provider_verdict"
        and row["raw"]["raw"]["verdict"] == "CONCERNS"
        for row in result["disposition"]["raw_verdicts"]
    )


@pytest.mark.parametrize(
    "change",
    ["top_projection", "container_projection", "top_manifest", "container_manifest", "raw_digest"],
)
def test_large_nonpass_projection_tamper_cannot_resolve(tmp_path, monkeypatch, change):
    from pathlib import Path

    import closer_disposition as disposition

    snap, packet, ref, _prep, _interpretation = large_nonpass_fixture(tmp_path, monkeypatch)
    content = packet["sections"]["native_merge_verifier"]["content"]
    container = content["artifacts"][0]["receipts"][0]
    if change == "top_projection":
        content["input_manifest_projection"]["omitted_field_count"] += 1
    elif change == "container_projection":
        container["manifest_projection"]["acceptance_authority"] = True
    elif change == "top_manifest":
        content["input_manifest"]["mode"] = "invented"
    elif change == "container_manifest":
        container["manifest"]["mode"] = "invented"
    else:
        content["input_manifest"]["raw"]["sha256"] = "0" * 64
    assert not disposition._native_resolution_index(
        packet, Path(ref["path"]).parent.parent, snap["verifier"]
    )
