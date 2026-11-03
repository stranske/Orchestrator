"""Build one fresh, closed public CSV qualification input without model authority.

The historical qualification packet was intentionally not reconstructed when its
temporary source disappeared.  This module instead admits one byte-pinned public
snapshot and asks the existing authenticated evidence collector to make a new
packet under a caller-supplied private root.  It has no provider, enrollment,
runtime, repair, or action authority.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import time
from pathlib import Path

SNAPSHOT_PATH = Path(
    "/private/tmp/closer-live-csv-historical-readback-source1030-20261030/"
    "campaigns/source984-dispatch-state.json.evidence/snapshots/"
    "df4531e8d692b71e0d91a6d0e5dc9f2b6d89ba03eb9a12437d72a19af914344c.json"
)
SNAPSHOT_SHA256 = "df4531e8d692b71e0d91a6d0e5dc9f2b6d89ba03eb9a12437d72a19af914344c"
SNAPSHOT_BYTES = 48769
PATCH_PATH = Path(
    "/private/tmp/closer-live-csv-historical-readback-source1030-20261030/"
    "campaigns/source984-dispatch-state.json.evidence/raw/69/"
    "690215f578b86592a34f214feededa53e55986ae7118266e8025cdc4af6ebfb3.patch"
)
PATCH_SHA256 = "690215f578b86592a34f214feededa53e55986ae7118266e8025cdc4af6ebfb3"
PATCH_BYTES = 431
ORIGINAL_REPORT_PATH = Path(
    "/Users/teacher/.codex/handoff/dispatch-logs/sonnet-role-qualification/"
    "c256f1adb15405e8aa82e2baab729c38f1a0edb9994c529079e99ec3d56358f2.result.json"
)
ORIGINAL_REPORT_SHA256 = "82ff806f22863e3f5fca5a942c2cdc666f100a6cfbb922f7f066b8e807ffb20e"
ORIGINAL_REPORT_BYTES = 93036
TARGET = "stranske/Orchestrator#984"
HEAD = "0507d28acf68d6bf21c68138d18d56cd01968827"
MERGE = "d1947b5b4775c2e6a07bb46b3f85eb2e28c1b001"
SOURCE = "stranske/Orchestrator#983"
SOURCE_SHA256 = "64a22ce25c538cfeb98c058fc66bca2165ac829df1c037e5b95cefa22959646c"
CSV_FILE = "tests/fixtures/closer_real_csv_canary/v1/csv_export.py"
CSV_CRITERION = (
    "CSV must emit exactly the header name,score, CRLF record endings and one record per input. "
    "Values containing commas, quotes, LF or CR must round-trip through Python csv.reader. "
    "Empty input must return the header alone."
)
QUESTION = "Does the retained public CSV canary implementation satisfy the original CSV round-trip criterion?"
_MAX_JSON_BYTES = 512 * 1024


class PublicQualificationInputUnavailable(ValueError):
    """The exact public seed or its new collected packet was not authentic."""


def _need(condition, reason):
    if not condition:
        raise PublicQualificationInputUnavailable(reason)


def _read_pinned(path, *, digest, size):
    """Read one owned, non-symlinked immutable seed without fallback paths."""
    path = Path(path)
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise PublicQualificationInputUnavailable("seed_path_changed") from error
    _need(path.is_absolute() and resolved == path, "seed_path_changed")
    _need(not any(parent.is_symlink() for parent in (path, *path.parents)), "seed_path_symlinked")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise PublicQualificationInputUnavailable("seed_path_changed") from error
    try:
        info = os.fstat(descriptor)
        _need(
            stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_size == size,
            "seed_file_metadata_changed",
        )
        raw = os.read(descriptor, size + 1)
    finally:
        os.close(descriptor)
    _need(len(raw) == size and hashlib.sha256(raw).hexdigest() == digest, "seed_bytes_changed")
    return raw


def _seed():
    """Return only the exact public snapshot, patch, and immutable original row."""
    import closer_claude_role_admission as admission

    snapshot_raw = _read_pinned(SNAPSHOT_PATH, digest=SNAPSHOT_SHA256, size=SNAPSHOT_BYTES)
    patch_raw = _read_pinned(PATCH_PATH, digest=PATCH_SHA256, size=PATCH_BYTES)
    report_raw = _read_pinned(
        ORIGINAL_REPORT_PATH, digest=ORIGINAL_REPORT_SHA256, size=ORIGINAL_REPORT_BYTES
    )
    try:
        snapshot = json.loads(snapshot_raw)
        report = json.loads(report_raw)
    except (TypeError, ValueError, UnicodeError) as error:
        raise PublicQualificationInputUnavailable("seed_json_invalid") from error
    sources = snapshot.get("sources") if isinstance(snapshot, dict) else None
    selected = [
        row for row in sources or [] if isinstance(row, dict) and row.get("target") == SOURCE
    ]
    _need(
        isinstance(snapshot, dict)
        and snapshot.get("target") == TARGET
        and snapshot.get("kind") == "pr"
        and snapshot.get("state") == "MERGED"
        and snapshot.get("head") == HEAD
        and snapshot.get("merge") == MERGE
        and len(selected) == 1
        and selected[0].get("kind") == "issue"
        and isinstance(selected[0].get("body"), str)
        and hashlib.sha256(selected[0]["body"].encode()).hexdigest() == SOURCE_SHA256,
        "public_snapshot_identity_changed",
    )
    proposal = report.get("proposal") if isinstance(report, dict) else None
    _need(
        report.get("schema") == admission.SCHEMA
        and report.get("capability_pass") is True
        and report.get("authority") is False
        and report.get("activation") is False
        and isinstance(proposal, dict)
        and proposal.get("schema") == admission.PROPOSAL_SCHEMA
        and isinstance(proposal.get("original"), dict)
        and isinstance(report.get("quality_ref"), dict),
        "immutable_original_report_changed",
    )
    return snapshot, patch_raw, proposal["original"], report["quality_ref"]


def _fresh_root(path):
    path = Path(path)
    _need(
        path.is_absolute() and not path.exists() and not path.is_symlink(),
        "fresh_private_root_required",
    )
    parent = path.parent
    _need(
        parent.is_dir()
        and not parent.is_symlink()
        and parent.stat().st_uid == os.getuid()
        and stat.S_IMODE(parent.stat().st_mode) == 0o700,
        "private_root_parent_required",
    )
    path.mkdir(mode=0o700)
    info = path.lstat()
    _need(
        stat.S_ISDIR(info.st_mode)
        and not path.is_symlink()
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o700,
        "fresh_private_root_invalid",
    )
    return path


def prepare_public_qualification_input(*, gh, private_root, deadline):
    """Collect one new, closed qualification proposal; never invoke a provider."""
    import closer_claude_role_admission as admission
    import closer_dispatcher
    import closer_disposition
    import closer_evidence

    _need(
        type(deadline) in (int, float) and math.isfinite(deadline) and time.monotonic() < deadline,
        "collection_deadline_invalid",
    )
    # All historical identities are checked before the caller's GitHub object is touched.
    snapshot, patch_raw, original, quality_ref = _seed()
    _need(hashlib.sha256(patch_raw).hexdigest() == PATCH_SHA256, "public_patch_changed")
    root = _fresh_root(private_root)
    packet = closer_dispatcher.collect_native_evidence(
        snapshot,
        gh,
        root,
        previous=None,
        deadline=deadline,
        dispositions={},
        action="collect_evidence",
    )
    _need(
        closer_evidence.validate_packet(packet)
        and packet.get("target") == TARGET
        and packet.get("head") == HEAD
        and packet.get("collection_action") == "collect_evidence",
        "collected_packet_identity_changed",
    )
    manifest = packet.get("manifest") or {}
    digest = manifest.get("packet_sha256")
    _need(isinstance(digest, str) and len(digest) == 64, "collected_packet_manifest_invalid")
    manifest_path = root / "manifests" / (digest + ".json")
    raw = _read_pinned(
        manifest_path,
        digest=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        size=manifest_path.stat().st_size,
    )
    packet_ref = {
        "path": str(manifest_path),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    index = closer_disposition._receipt_index(packet, root)
    matches = []
    for (path, digest_value), rows in index.items():
        entries = rows if isinstance(rows, list) else [rows]
        if (
            digest_value == PATCH_SHA256
            and len(entries) == 1
            and entries[0].get("kind") == "codepatch"
            and entries[0].get("filename") == CSV_FILE
        ):
            item = Path(path)
            matches.append(
                {
                    "path": path,
                    "bytes": item.stat().st_size,
                    "sha256": digest_value,
                }
            )
    _need(len(matches) == 1, "exact_public_patch_not_collected")
    proposal = {
        "schema": admission.PROPOSAL_SCHEMA,
        "snapshot": snapshot,
        "packet_ref": packet_ref,
        "original": original,
        "basis": {"source": SOURCE, "criterion": CSV_CRITERION},
        "evidence_refs": matches,
    }
    # The admission's own exact contract is the last source-side replay fence.
    admission._prepare(proposal)
    return {
        "proposal": proposal,
        "quality_ref": quality_ref,
        "authority": False,
        "activation": False,
        "provider_invoked": False,
        "enrollment_performed": False,
    }
