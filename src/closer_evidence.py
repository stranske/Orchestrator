"""Bounded, provenance-bearing evidence packets for closer admission.

This module only observes existing GitHub state. It never starts CI or verifier runs.
Untrusted issue, review, artifact and disposition text is retained as data, never instructions.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import quote

SCHEMA = "closer-evidence/v1"
PRODUCER_VERSION = "1.2.0"
MAX_SECTION_BYTES = 256_000
MAX_TEXT_BYTES = 32_000
MAX_SOURCES = 20
MAX_THREADS = 100
MAX_COMMENTS_PER_THREAD = 100
MAX_RUN_PAGES = 3
MAX_RUNS = 150
MAX_FILES = 200
MAX_ARCHIVE_BYTES = 8_000_000
MAX_UNCOMPRESSED_BYTES = 16_000_000
# Canonical sealed transport inputs are not model section content.
MAX_NATIVE_INPUT_BYTES = MAX_UNCOMPRESSED_BYTES
NATIVE_INPUT_COMPONENTS = frozenset(
    {
        "verifier-input-manifest.json",
        "verifier-context.md",
        "verifier-pr-diff.patch",
        "verifier-diff-summary.md",
        "closer-correction-seeds.json",
    }
)
MAX_ARTIFACTS = 20
MAX_PACKET_ARCHIVES = 20
MAX_PACKET_ARCHIVE_BYTES = 8_000_000
MAX_NATIVE_RESULTS = 100
MAX_NATIVE_CONCERNS = 500
SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_SECTIONS = {
    "source",
    "diff_files",
    "review_conversations",
    "prior_dispositions",
    "checks",
    "ci_artifacts",
    "test_coverage_artifacts",
    "native_merge_verifier",
}
VALID_STATUS = {"complete", "partial", "pending", "missing", "unknown", "truncated"}


class RoundControlException:
    """Marker for scheduler-level deadline/capacity control-flow exceptions."""


def _bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".evidence-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _retain_bytes(root: Path, data: bytes, suffix: str) -> dict:
    sha = hashlib.sha256(data).hexdigest()
    path = root / "raw" / sha[:2] / f"{sha}.{suffix}"
    if not path.exists():
        _atomic_write(path, data)
    return {"sha256": sha, "path": str(path), "bytes": len(data)}


def _retained_ref_valid(ref: Any) -> bool:
    if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
        return False
    try:
        data = Path(ref["path"]).read_bytes()
    except OSError:
        return False
    return len(data) == ref.get("bytes") and hashlib.sha256(data).hexdigest() == ref.get("sha256")


def _retained_section_valid(section: Any, *, allow_partial: bool = False) -> bool:
    if not isinstance(section, dict):
        return False
    if section.get("status") != "complete" and not (
        allow_partial and section.get("status") == "partial" and section.get("truncated") is False
    ):
        return False
    return _retained_section_payload_valid(section)


def _retained_section_payload_valid(section: dict) -> bool:
    """Check immutable storage without granting any completeness status."""
    if section.get("sha256") != _section_hash(section):
        return False
    ref = section.get("retained")
    if not _retained_ref_valid(ref):
        return False
    if not isinstance(ref, dict):
        return False
    try:
        raw = json.loads(Path(ref["path"]).read_bytes())
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    expected = {k: v for k, v in section.items() if k != "retained"}
    return raw == expected


def _redaction_only_review(section: Any) -> bool:
    if (
        not isinstance(section, dict)
        or section.get("status") != "partial"
        or section.get("reason") != "credential-like values redacted"
        or section.get("truncated") is not False
        or section.get("provenance", {}).get("pagination_complete") is not True
        or not _retained_section_valid(section, allow_partial=True)
    ):
        return False
    rows = section.get("content")
    if not isinstance(rows, list) or not rows:
        return False
    comments = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or row.get("comments_complete") is not True
            or row.get("comments_has_next_page") is True
            or not isinstance(row.get("comments"), list)
        ):
            return False
        comments.extend(row["comments"])
    return (
        bool(comments)
        and any(isinstance(c, dict) and c.get("redaction_applied") is True for c in comments)
        and all(
            isinstance(c, dict)
            and c.get("truncated") is False
            and isinstance(c.get("body_retained"), dict)
            and c["body_retained"].get("truncated") is False
            for c in comments
        )
    )


def _safe_text(value: Any, limit: int = MAX_TEXT_BYTES) -> tuple[str, bool]:
    # Preserve original content. Do not redact ordinary acceptance prose and never
    # silently mutate source text; credential-like patterns are flagged by callers.
    text = str(value or "")
    raw = text.encode("utf-8", "replace")
    return (text, False) if len(raw) <= limit else (raw[:limit].decode("utf-8", "ignore"), True)


def _has_credential_pattern(text: str) -> bool:
    return bool(
        re.search(r"(?i)(?:token|password|secret|authorization|api[_-]?key)(\s*[:=]\s*)\S+", text)
    )


def _redact_credentials(text: str) -> tuple[str, bool]:
    pattern = re.compile(
        r"(?i)((?:token|password|secret|authorization|api[_-]?key)(\s*[:=]\s*))([^\s,;]+)"
    )
    return pattern.subn(r"\1[REDACTED]", text)[0], bool(pattern.search(text))


def _title_transform(original: str) -> dict:
    """Bind original and masked display bytes without claiming they are equal.

    No original masked text is copied into an alternate field. This attests the
    acquisition transform, never that unknown title meaning was preserved.
    """
    bounded, cut = _safe_text(original)
    displayed, redacted = _redact_credentials(bounded)
    return {
        "schema": "closer-source-title-transform/v1",
        "original_sha256": hashlib.sha256(original.encode("utf-8", "replace")).hexdigest(),
        "display_sha256": hashlib.sha256(displayed.encode("utf-8", "replace")).hexdigest(),
        "redaction_applied": redacted,
        "truncated": cut,
        "meaning_status": "unavailable" if cut or redacted else "original",
        "acceptance_authority": False,
        "text_is_untrusted": True,
    }


def _title_provenance_valid(row: dict, source: dict, *, allow_display_transform=False) -> bool:
    original = source.get("title") or ""
    expected = _title_transform(original)
    displayed, _ = _redact_credentials(_safe_text(original)[0])
    ref = row.get("title_retained")
    if not isinstance(ref, dict):
        return False
    if "title_transform" not in row:
        # Existing untransformed packets retain their exact original-byte contract.
        return bool(
            row.get("title") == original
            and row.get("title_sha256") == hashlib.sha256(original.encode()).hexdigest()
            and _retained_ref_valid(ref)
            and ref.get("sha256") == row.get("title_sha256")
            and ref.get("bytes") == len(original.encode())
            and ref.get("truncated") is False
        )
    return bool(
        row.get("title_transform") == expected
        and (expected["meaning_status"] == "original" or allow_display_transform)
        and row.get("title") == displayed
        and row.get("title_sha256") == expected["original_sha256"]
        and _retained_ref_valid(ref)
        and ref.get("sha256") == expected["display_sha256"]
        and ref.get("bytes") == len(displayed.encode("utf-8", "replace"))
        and ref.get("truncated") is False
    )


def _merge_title_display_only(section: dict, sources) -> bool:
    """Objective merge can evaluate a complete body without knowing masked title meaning."""
    rows = section.get("content")
    if not (
        section.get("status") == "partial"
        and section.get("reason") == "credential-like values redacted"
        and section.get("provenance", {}).get("source_digest") == _hash(sources)
        and _retained_section_valid(section, allow_partial=True)
        and isinstance(sources, list)
        and sources
        and isinstance(rows, list)
        and len(rows) == len(sources)
    ):
        return False
    return all(
        isinstance(row, dict)
        and isinstance(source, dict)
        and row.get("target") == source.get("target")
        and isinstance(source.get("body"), str)
        and source["body"].strip()
        and row.get("body") == source["body"]
        and row.get("body_sha256") == hashlib.sha256(source["body"].encode()).hexdigest()
        and _retained_ref_valid(row.get("body_retained"))
        and row["body_retained"].get("sha256") == row["body_sha256"]
        and row["body_retained"].get("truncated") is False
        and _title_provenance_valid(row, source, allow_display_transform=True)
        for row, source in zip(rows, sources)
    )


def _section(
    status: str,
    content: Any = None,
    *,
    provenance: Any = None,
    reason: str | None = None,
    truncated: bool = False,
) -> dict:
    section = {
        "status": status,
        "content": content,
        "provenance": provenance or {},
        "reason": reason,
        "truncated": bool(truncated),
    }
    if len(_bytes(section)) > MAX_SECTION_BYTES:
        section.update(
            status="truncated", content=None, reason="section byte limit exceeded", truncated=True
        )
    section["sha256"] = _hash(section)
    return section


def _section_hash(section: dict) -> str:
    return _hash({k: v for k, v in section.items() if k not in {"sha256", "retained"}})


def _subject_target(snapshot: dict) -> str:
    """Resolve delivery PR identity while preserving an assigned issue target."""
    candidate = (
        snapshot.get("pr_target")
        if snapshot.get("kind") == "pr" and snapshot.get("pr_target")
        else snapshot.get("target")
    )
    if not isinstance(candidate, str) or not re.fullmatch(r"[^/#]+/[^#]+#[1-9][0-9]*", candidate):
        return str(snapshot.get("target") or "")
    return candidate


def validate_patch_reference(file_row: dict, *, base: str, head: str) -> bool:
    """Verify a retained full patch is still byte-identical and identity-bound."""
    if (
        not isinstance(file_row, dict)
        or file_row.get("patch_missing_or_binary")
        or file_row.get("truncated")
    ):
        return False
    binding = file_row.get("patch_binding") or {}
    if binding.get("base") != base or binding.get("head") != head or not binding.get("filename"):
        return False
    ref = file_row.get("patch_retained")
    patch = file_row.get("patch")
    if (
        not isinstance(ref, dict)
        or not isinstance(patch, str)
        or not isinstance(ref.get("path"), str)
    ):
        return False
    try:
        raw = Path(ref["path"]).read_bytes()
    except OSError:
        return False
    expected = hashlib.sha256(raw).hexdigest()
    return (
        raw == patch.encode("utf-8")
        and len(raw) == ref.get("bytes")
        and expected == ref.get("sha256") == file_row.get("patch_sha256")
    )


def validate_packet(packet: Any) -> bool:
    """Strict structural/integrity validation; semantic acceptance is separate."""
    if (
        not isinstance(packet, dict)
        or packet.get("schema") != SCHEMA
        or packet.get("producer_version") != PRODUCER_VERSION
    ):
        return False
    if not isinstance(packet.get("producer_sha256"), str) or not SHA256.fullmatch(
        packet["producer_sha256"]
    ):
        return False
    if not isinstance(packet.get("target"), str) or not re.fullmatch(
        r"[^/#]+/[^#]+#[1-9][0-9]*", packet["target"]
    ):
        return False
    for key in ("head", "base"):
        if packet.get(key) is not None and (
            not isinstance(packet[key], str) or not SHA.fullmatch(packet[key])
        ):
            return False
    subject = packet.get("subject_pr")
    if subject is not None and (
        not isinstance(subject, str) or not re.fullmatch(r"[^/#]+/[^#]+#[1-9][0-9]*", subject)
    ):
        return False
    sections = packet.get("sections")
    digests = packet.get("section_digests")
    bindings = packet.get("bindings")
    if not isinstance(sections, dict) or not REQUIRED_SECTIONS.issubset(sections):
        return False
    if not isinstance(digests, dict) or not isinstance(bindings, dict) or not bindings:
        return False
    if bindings.get("identity") != _hash(
        {
            "target": packet["target"],
            "subject_pr": subject,
            "head": packet.get("head"),
            "base": packet.get("base"),
        }
    ):
        return False
    for name, section in sections.items():
        if not isinstance(section, dict) or section.get("status") not in VALID_STATUS:
            return False
        if section.get("sha256") != _section_hash(section) or digests.get(name) != section.get(
            "sha256"
        ):
            return False
        if not isinstance(section.get("provenance"), dict):
            return False
    if set(digests) != set(sections):
        return False
    if packet.get("semantic_cache_key") != _semantic_cache_key(packet):
        return False
    manifest = packet.get("manifest")
    if manifest is not None:
        if not isinstance(manifest, dict) or not isinstance(manifest.get("packet_sha256"), str):
            return False
        if not isinstance(manifest.get("section_files"), dict) or set(
            manifest["section_files"]
        ) != set(sections):
            return False
        if manifest.get("packet_sha256") != _packet_hash(packet):
            return False
        if any(
            manifest["section_files"].get(name) != section.get("retained")
            for name, section in sections.items()
        ):
            return False
    collection = packet.get("collection_result")
    if collection is not None:
        if (
            not isinstance(collection, dict)
            or collection.get("schema") != "closer-evidence-collection/v1"
            or collection.get("status") not in {"ready", "pending", "diagnostic", "deterministic"}
            or collection.get("evaluation_ready") is not (collection.get("status") == "ready")
            or collection.get("acceptance_authority") is not False
            or collection.get("action") != packet.get("collection_action")
            or not isinstance(collection.get("readiness"), dict)
        ):
            return False
        readiness = collection["readiness"]
        if (
            readiness.get("schema") != "closer-action-evidence-readiness/v1"
            or readiness.get("action") != collection["action"]
            or (
                collection["status"] == "ready"
                and (
                    readiness.get("ready") is not True
                    or readiness.get("mode")
                    not in {"acceptance", "repair", "review", "nonpass_triage"}
                )
            )
            or (collection["status"] == "pending" and readiness.get("ready") is not False)
            or (collection["status"] == "diagnostic" and collection["action"] is not None)
        ):
            return False
    return True


def action_evidence_readiness(
    snapshot: Any, packet: Any, action: Any, *, retained_dir: str | Path | None = None
) -> dict:
    try:
        verdict = _action_evidence_readiness(snapshot, packet, action, retained_dir=retained_dir)
        from closer_evidence_recovery import classify_requirements

        return classify_requirements(verdict, snapshot, packet, retained_dir=retained_dir)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        return {
            "schema": "closer-action-evidence-readiness/v1",
            "ready": False,
            "action": action,
            "mode": "unavailable",
            "gaps": ["evidence:malformed_or_unavailable_raw_inputs"],
        }


def _action_evidence_readiness(
    snapshot: Any, packet: Any, action: Any, *, retained_dir: str | Path | None = None
) -> dict:
    """Admit useful model work, never acceptance, from existing typed raw inputs.

    Packet integrity alone permits missing sections. This separate rail refuses
    known collection gaps before spending on their semantic interpretation. It
    does not infer which tests a prose criterion needs or certify their sufficiency.
    """
    verdict: dict[str, Any] = {
        "schema": "closer-action-evidence-readiness/v1",
        "ready": False,
        "action": action,
        "mode": "acceptance",
        "gaps": [],
    }
    gaps: list[str] = verdict["gaps"]
    try:
        valid = validate_packet(packet)
    except (ValueError, TypeError, KeyError, AttributeError):
        valid = False
    if not valid or not isinstance(snapshot, dict):
        gaps.append("packet:invalid_or_unavailable")
        return verdict
    if action in {"complete", "wait", "post_merge_verify"}:
        verdict.update(ready=True, mode="deterministic")
        return verdict
    if action not in {
        "collect_evidence",
        "merge",
        "close_source",
        "review",
        "respond_review",
        "repair",
        "disposition",
    }:
        gaps.append("action:unsupported_model_assessment")
    subject = _subject_target(snapshot)
    if snapshot.get("kind") != "pr" or packet.get("subject_pr") != subject:
        inventory = snapshot.get("delivery_links") or {}
        if isinstance(inventory, dict):
            verdict["delivery_links"] = inventory
        status = (
            inventory.get("status", "unidentified")
            if isinstance(inventory, dict)
            else "unidentified"
        )
        gaps.append("delivery_pr:" + str(status))
    if packet.get("target") != snapshot.get("target"):
        gaps.append("packet:target_mismatch")
    for key in ("head", "base"):
        value = snapshot.get(key)
        if not isinstance(value, str) or not SHA.fullmatch(value):
            gaps.append("delivery:" + key + "_missing")
        elif packet.get(key) != value:
            gaps.append("packet:" + key + "_mismatch")
    sections = packet["sections"]

    def retained(name: str, *, partial: bool = False) -> bool:
        section = sections.get(name)
        if not _retained_section_valid(section, allow_partial=partial):
            status = section.get("status") if isinstance(section, dict) else "missing"
            gaps.append(name + ":unavailable_or_unverified:" + str(status))
            verdict.setdefault("section_reasons", {})[name] = {
                "status": status,
                "reason": section.get("reason") if isinstance(section, dict) else "section missing",
            }
            return False
        return True

    sources = snapshot.get("sources")
    if not isinstance(sources, list) or not sources:
        gaps.append("source:original_inventory_missing")
    elif packet["bindings"].get("source") != _hash(sources):
        gaps.append("source:inventory_binding_mismatch")
    original_source = sections.get("source") or {}
    merge_title_display_only = action == "merge" and _merge_title_display_only(
        original_source, sources
    )
    if merge_title_display_only:
        verdict["unresolved_title_meaning"] = [
            {
                "target": row["target"],
                "gap": "source:title_meaning_unavailable",
                "necessity": "useful",
                "transform": row["title_transform"],
                "acceptance_authority": False,
            }
            for row in original_source["content"]
            if row.get("title_transform", {}).get("meaning_status") == "unavailable"
        ]
    if (
        not merge_title_display_only
        and original_source.get("status") in {"partial", "truncated"}
        and original_source.get("reason")
        in {
            "credential-like values redacted",
            "source count/body byte bound reached",
            "section byte limit exceeded",
        }
        and original_source.get("provenance", {}).get("source_digest") == _hash(sources)
        and _retained_section_payload_valid(original_source)
    ):
        verdict["mode"] = "source_transform"
        verdict["dependency"] = {
            "schema": "closer-source-transform-dependency/v1",
            "kind": "senior_source_transform",
            "target": snapshot.get("target"),
            "revision": snapshot.get("revision"),
            "source_status": original_source["status"],
            "reason": original_source["reason"],
            "source_ref": original_source["retained"],
            "source_sha256": original_source["sha256"],
            "original_source_sha256": packet["bindings"].get("source"),
            "next_trigger": "senior source resolution supplies a complete criteria-preserving safely representable original source bound to this source identity",
        }
    if retained("source", partial=merge_title_display_only):
        rows = sections["source"].get("content")
        if (
            not isinstance(rows, list)
            or not rows
            or not isinstance(sources, list)
            or [row.get("target") for row in rows if isinstance(row, dict)]
            != [row.get("target") for row in sources if isinstance(row, dict)]
            or sections["source"].get("provenance", {}).get("source_digest") != _hash(sources)
            or any(
                not isinstance(row, dict)
                or not row.get("body")
                or row.get("body") != source.get("body")
                or row.get("body_sha256") != hashlib.sha256(row["body"].encode()).hexdigest()
                or not _retained_ref_valid(row.get("body_retained"))
                or row["body_retained"].get("sha256") != row.get("body_sha256")
                or row["body_retained"].get("bytes") != len(row["body"].encode())
                or row.get("body_retained", {}).get("truncated") is not False
                or (
                    "title" in source
                    and not _title_provenance_valid(
                        row, source, allow_display_transform=merge_title_display_only
                    )
                )
                for row, source in zip(rows, sources)
            )
        ):
            gaps.append("source:original_bodies_unavailable")
    if gaps:
        return verdict
    if action == "merge":
        # Only identity and preserved original request are packet prerequisites.
        # Objective live merge checks remain in the native selector/merge guard;
        # post-verification acceptance proof is not a pre-merge evidence rail.
        verdict.update(ready=True, mode="deterministic")
        return verdict
    if action in {"review", "respond_review"}:
        verdict["mode"] = "review"
        if packet["bindings"].get("reviews") != _hash(snapshot.get("review_revision")):
            gaps.append("review_conversations:revision_binding_mismatch")
        if retained(
            "review_conversations",
            partial=_redaction_only_review(sections.get("review_conversations")),
        ):
            reviews = sections["review_conversations"]
            comments = [
                comment for row in reviews.get("content", []) for comment in row["comments"]
            ]
            if (
                reviews.get("provenance", {}).get("review_revision")
                != snapshot.get("review_revision")
                or reviews.get("provenance", {}).get("pagination_complete") is not True
                or reviews.get("truncated") is not False
                or any(
                    row.get("comments_complete") is not True
                    or row.get("comments_has_next_page") is True
                    for row in reviews.get("content", [])
                )
                or not comments
                or any(
                    not isinstance(comment.get("body"), str)
                    or comment.get("truncated") is not False
                    or not _retained_ref_valid(comment.get("body_retained"))
                    or comment["body_retained"].get("truncated") is not False
                    or comment["body_retained"].get("sha256")
                    != hashlib.sha256(comment["body"].encode()).hexdigest()
                    or comment["body_retained"].get("bytes") != len(comment["body"].encode())
                    for comment in comments
                )
            ):
                gaps.append("review_conversations:raw_comments_missing_or_unbound")
    else:
        from closer_disposition import (
            _native_resolution_index,
            _native_section_metadata,
            _receipt_index,
        )

        root = Path(retained_dir) if retained_dir is not None else None
        verifier = snapshot.get("verifier")
        import verifier_evidence

        repo, number = subject.rsplit("#", 1)
        trusted_identity = verifier_evidence.decision_matches_identity(
            verifier, repo, int(number), snapshot.get("head"), snapshot.get("merge")
        )
        if snapshot.get("state") == "MERGED" and not trusted_identity:
            verdict["mode"] = "native_diagnostic"
            native = sections.get("native_merge_verifier") or {}
            verdict["section_reasons"] = {
                "native_merge_verifier": {
                    "status": native.get("status"),
                    "reason": native.get("reason"),
                }
            }
            gaps.append("native_merge_verifier:trusted_bound_verdict_missing")
        elif isinstance(verifier, dict) and verifier.get("verdict") not in {None, "PASS"}:
            verdict["mode"] = "nonpass_triage"
            native = sections.get("native_merge_verifier") or {}
            verdict["section_reasons"] = {
                "native_merge_verifier": {
                    "status": native.get("status"),
                    "reason": native.get("reason"),
                }
            }
            if (
                root is None
                or _native_section_metadata(packet, verifier) is None
                or not retained("native_merge_verifier", partial=True)
                or not _native_resolution_index(packet, root, verifier)
            ):
                gaps.append("native_merge_verifier:bound_raw_nonpass_missing")
        else:
            verdict["mode"] = "repair" if action == "repair" else "acceptance"
            diff_ok = retained("diff_files", partial=True)
            diff = sections["diff_files"].get("content")
            if diff_ok and (
                not isinstance(diff, dict)
                or diff.get("base") != packet["base"]
                or diff.get("head") != packet["head"]
            ):
                gaps.append("diff_files:delivery_binding_mismatch")
            if retained("checks"):
                checks = sections["checks"]
                if (
                    checks.get("provenance", {}).get("head_bound") is not True
                    or checks.get("content", {}).get("head") != packet["head"]
                    or checks.get("content", {}).get("aggregate_state") is None
                    or checks.get("content", {}).get("aggregate_state") != snapshot.get("checks")
                ):
                    gaps.append("checks:current_head_binding_missing")
            index = _receipt_index(packet, root) if root is not None else {}
            # Reuse the existing per-file admission rules, including usable raw
            # receipts inside partial envelopes. Neither all-section completion
            # nor an invented required-check topology is an admission condition.
            if not index:
                gaps.append("evidence:usable_raw_code_or_ci_missing")
    verdict["ready"] = not gaps
    return verdict


def readiness_summary(readiness: dict, *, packet_ref=None, inventory_ref=None) -> dict:
    """Bound callback metadata; immutable packet/section refs retain all proof."""
    summary = {
        key: readiness[key]
        for key in (
            "schema",
            "ready",
            "evaluation_ready",
            "requirements",
            "action",
            "mode",
            "gaps",
            "section_reasons",
            "dependency",
            "unresolved_title_meaning",
        )
        if key in readiness
    }
    summary.update(packet_ref=packet_ref, inventory_ref=inventory_ref)
    inventory = readiness.get("delivery_links")
    if isinstance(inventory, dict):
        candidates = inventory.get("candidates")
        candidates = candidates if isinstance(candidates, list) else []
        summary["delivery_links"] = {
            key: inventory.get(key) for key in ("status", "reason", "source", "complete")
        }
        summary["delivery_links"].update(
            candidates=candidates[:8],
            candidate_count=len(candidates),
            candidates_complete=len(candidates) <= 8,
            summary_only=True,
            inventory_ref=inventory_ref,
        )
    return summary


def collection_summary(packet: dict, packet_ref: dict) -> dict | None:
    collection = packet.get("collection_result")
    if not isinstance(collection, dict):
        return None
    inventory_ref = (packet.get("sections", {}).get("delivery_inventory") or {}).get("retained")
    summary = {
        key: collection[key]
        for key in ("schema", "status", "action", "evaluation_ready", "acceptance_authority")
    }
    summary["readiness"] = readiness_summary(
        collection["readiness"], packet_ref=packet_ref, inventory_ref=inventory_ref
    )
    return summary


def update_packet(
    previous: dict | None, fresh: dict, *, dependencies: dict[str, str] | None = None
) -> dict:
    """Refresh evidence every run; reuse only stable source/disposition sections.

    Volatile sections (CI runs, checks, review conversations) are deliberately never
    inherited: run-attempt state and comments can change without a changed PR SHA.
    """
    if not validate_packet(fresh):
        raise ValueError("fresh evidence packet is invalid")
    if not validate_packet(previous):
        return fresh
    assert isinstance(previous, dict)
    if any(
        previous.get(k) != fresh.get(k)
        for k in ("producer_version", "producer_sha256", "target", "subject_pr", "head", "base")
    ):
        return fresh
    reusable = {"source": "source", "prior_dispositions": "dispositions"}
    out = dict(fresh)
    sections = dict(fresh["sections"])
    for section_name, binding_name in reusable.items():
        old = previous["sections"].get(section_name)
        if (
            old
            and previous["bindings"].get(binding_name) == fresh["bindings"].get(binding_name)
            and _retained_section_valid(old, allow_partial=section_name == "source")
        ):
            sections[section_name] = old
    out["sections"] = sections
    out["section_digests"] = {k: v["sha256"] for k, v in sections.items()}
    out["history"] = (
        list(previous.get("history", [])) + [{"packet_sha256": _packet_hash(previous)}]
    )[-100:]
    return out


def _packet_hash(packet: dict) -> str:
    return _hash({k: v for k, v in packet.items() if k != "manifest"})


def _semantic_cache_key(packet: dict) -> str:
    semantic_sections = {
        name: {"status": section.get("status"), "sha256": section.get("sha256")}
        for name, section in packet.get("sections", {}).items()
        if name != "elapsed"
    }
    payload = {
        "schema": "closer-evidence-semantic/v1",
        "target": packet.get("target"),
        "subject_pr": packet.get("subject_pr"),
        "head": packet.get("head"),
        "base": packet.get("base"),
        "sections": semantic_sections,
    }
    if "collection_action" in packet:
        payload["collection_action"] = packet["collection_action"]
    return "closer-evidence-semantic/v1:" + _hash(payload)


def _requirements(body: str) -> list[str]:
    found, section = [], ""
    for line in body.splitlines():
        if line.startswith("#"):
            section = line.lower()
        elif (
            any(word in section for word in ("task", "acceptance", "success criteria"))
            and line.strip()
        ):
            found.append(re.sub(r"^\s*(?:[-*+] |\d+[.)] )(?:\[[ xX]\]\s*)?", "", line).strip())
    return found[:100]


def _source_section(sources: list, root: Path) -> dict:
    if not sources:
        return _section("missing", reason="original source unavailable")
    rows, truncated_any, redacted_any = [], len(sources) > MAX_SOURCES, False
    for source in sources[:MAX_SOURCES]:
        original = str(source.get("body") or "")
        original_bytes = original.encode("utf-8", "replace")
        body, cut = _safe_text(original)
        body, redacted = _redact_credentials(body)
        retained_bytes = body.encode("utf-8", "replace")[:MAX_TEXT_BYTES]
        ref = _retain_bytes(root, retained_bytes, "txt")
        original_title = str(source.get("title") or "")
        title, title_cut = _safe_text(original_title)
        title, title_redacted = _redact_credentials(title)
        title_bytes = original_title.encode("utf-8", "replace")
        title_transform = _title_transform(original_title)
        title_ref = _retain_bytes(root, title.encode("utf-8", "replace")[:MAX_TEXT_BYTES], "txt")
        credential_signal = (
            redacted
            or title_redacted
            or _has_credential_pattern(body)
            or _has_credential_pattern(title)
        )
        rows.append(
            {
                "target": source.get("target"),
                "kind": source.get("kind", "issue"),
                "provenance": source.get("provenance"),
                "request_revision": source.get("request_revision"),
                "title": title,
                "title_sha256": hashlib.sha256(title_bytes).hexdigest(),
                "title_transform": title_transform,
                "title_retained": {
                    **title_ref,
                    "truncated": title_cut or len(title_bytes) > MAX_TEXT_BYTES,
                },
                "body": body,
                "body_sha256": hashlib.sha256(original_bytes).hexdigest(),
                "body_retained": {**ref, "truncated": cut or len(original_bytes) > MAX_TEXT_BYTES},
                "requirements": _requirements(body),
                "state": source.get("state"),
                "labels": source.get("labels", [])[:100],
                "truncated": cut or title_cut,
                "credential_pattern_present": credential_signal,
                "redaction_applied": redacted or title_redacted,
                "text_is_untrusted": True,
            }
        )
        truncated_any |= cut or title_cut
        redacted_any |= redacted or title_redacted
    status = "truncated" if truncated_any else "partial" if redacted_any else "complete"
    return _section(
        status,
        rows,
        provenance={"source_inventory_count": len(sources), "source_digest": _hash(sources)},
        reason=(
            "source count/body byte bound reached"
            if truncated_any
            else "credential-like values redacted" if redacted_any else None
        ),
        truncated=truncated_any,
    )


def _diff_section(snapshot: dict, api: Callable, deadline: float, root: Path) -> dict:
    target = _subject_target(snapshot)
    repo = target.split("#", 1)[0]
    base, head = snapshot.get("base"), snapshot.get("head")
    if not repo or not (
        isinstance(base, str)
        and SHA.fullmatch(base)
        and isinstance(head, str)
        and SHA.fullmatch(head)
    ):
        return _section("missing", reason="base or head commit unavailable; diff unknown")
    try:
        if time.monotonic() >= deadline:
            return _section("pending", reason="collection deadline reached")
        compare = api(f"repos/{repo}/compare/{base}...{head}")
        all_files = compare.get("files") or []
        rows, partial = [], len(all_files) >= 300 or len(all_files) > MAX_FILES
        for item in all_files[:MAX_FILES]:
            filename = str(item.get("filename") or "")
            patch = item.get("patch")
            text, cut = _safe_text(patch or "", MAX_TEXT_BYTES)
            status = item.get("status")
            binary_or_patch_omitted = patch is None
            patch_retained = None
            patch_sha = None
            if patch is not None:
                patch_bytes = str(patch).encode("utf-8", "replace")
                patch_sha = hashlib.sha256(patch_bytes).hexdigest()
                if not cut:
                    patch_retained = _retain_bytes(root, patch_bytes, "patch")
            rows.append(
                {
                    "filename": filename,
                    "status": status,
                    "additions": item.get("additions"),
                    "deletions": item.get("deletions"),
                    "changes": item.get("changes"),
                    "blob_sha": item.get("sha"),
                    "patch": text if patch is not None else None,
                    "patch_sha256": patch_sha,
                    "patch_retained": patch_retained,
                    "patch_binding": {
                        "base": base,
                        "head": head,
                        "filename": filename,
                        "blob_sha": item.get("sha"),
                    },
                    "patch_missing_or_binary": binary_or_patch_omitted,
                    "truncated": cut,
                }
            )
            partial |= cut or binary_or_patch_omitted
        status = (
            "truncated"
            if len(all_files) >= 300 or len(all_files) > MAX_FILES
            else "partial" if partial else "complete"
        )
        return _section(
            status,
            {
                "base": base,
                "head": head,
                "compare_status": compare.get("status"),
                "ahead_by": compare.get("ahead_by"),
                "files": rows,
                "file_count_observed": len(all_files),
            },
            provenance={"source": "GitHub compare API", "base": base, "head": head},
            reason=(
                "compare file/patch bound reached or some patches unavailable" if partial else None
            ),
            truncated=status == "truncated",
        )
    except Exception as exc:
        if _is_round_control_exception(exc):
            raise
        return _section(
            "unknown",
            reason=f"compare lookup failed: {type(exc).__name__}",
            provenance={"base": base, "head": head},
        )


def _review_section(snapshot: dict, review_threads_fetch, deadline: float, root: Path) -> dict:
    if snapshot.get("kind") != "pr":
        return _section(
            "missing",
            reason="PR review history is not applicable without an identified delivery PR",
            provenance={"applicable": False, "source": "issue-only delivery inventory"},
        )
    threads = snapshot.get("threads")
    if review_threads_fetch is not None:
        try:
            if time.monotonic() >= deadline:
                return _section("pending", reason="collection deadline reached")
            result = review_threads_fetch(
                snapshot,
                deadline=deadline,
                max_pages=5,
                max_threads=MAX_THREADS,
                max_comments_per_thread=MAX_COMMENTS_PER_THREAD,
            )
            if not isinstance(result, dict) or not isinstance(result.get("threads"), list):
                return _section("unknown", reason="thread fetch callback returned invalid shape")
            threads = result["threads"]
            complete = result.get("complete") is True and result.get("has_next_page") is False
            cursor = result.get("end_cursor")
        except Exception as exc:
            if _is_round_control_exception(exc):
                raise
            return _section("unknown", reason=f"thread comment lookup failed: {type(exc).__name__}")
    else:
        complete = False
        cursor = None
    if not isinstance(threads, list):
        return _section("unknown", reason="review thread history missing")
    rows, truncated, redacted_any = [], len(threads) > MAX_THREADS, False
    for thread in threads[:MAX_THREADS]:
        comments_obj = thread.get("comments") or {}
        nodes = comments_obj.get("nodes") if isinstance(comments_obj, dict) else comments_obj
        if not isinstance(nodes, list):
            nodes = []
        page_info = comments_obj.get("pageInfo", {}) if isinstance(comments_obj, dict) else {}
        comments_complete = complete or (
            page_info.get("hasNextPage") is False and page_info.get("hasPreviousPage") is False
        )
        if len(nodes) > MAX_COMMENTS_PER_THREAD:
            truncated = True
        comment_rows = []
        for comment in nodes[:MAX_COMMENTS_PER_THREAD]:
            text, cut = _safe_text(comment.get("body"))
            raw = str(comment.get("body") or "").encode("utf-8", "replace")
            safe_raw, redacted = _redact_credentials(text)
            safe_bytes = safe_raw.encode("utf-8", "replace")[:MAX_TEXT_BYTES]
            retained = _retain_bytes(root, safe_bytes, "txt")
            redacted_any |= redacted
            comment_rows.append(
                {
                    "id": comment.get("id"),
                    "author": (comment.get("author") or {}).get("login"),
                    "body": safe_bytes.decode("utf-8", "ignore"),
                    "body_sha256": hashlib.sha256(raw).hexdigest(),
                    "body_retained": {**retained, "truncated": cut or len(raw) > MAX_TEXT_BYTES},
                    "redaction_applied": redacted,
                    "createdAt": comment.get("createdAt"),
                    "updatedAt": comment.get("updatedAt"),
                    "truncated": cut,
                    "text_is_untrusted": True,
                }
            )
            truncated |= cut
        rows.append(
            {
                "id": thread.get("id"),
                "resolved": thread.get("isResolved"),
                "outdated": thread.get("isOutdated"),
                "comments": comment_rows,
                "comments_complete": comments_complete,
                "comments_has_next_page": page_info.get("hasNextPage"),
            }
        )
        if not comments_complete:
            truncated = True
    status = (
        "truncated"
        if truncated
        else "partial" if redacted_any else "complete" if complete else "partial"
    )
    return _section(
        status,
        rows,
        provenance={
            "review_revision": snapshot.get("review_revision"),
            "pagination_complete": complete,
            "end_cursor": cursor,
            "snapshot_threads_count": len(snapshot.get("threads") or []),
        },
        reason=(
            None
            if status == "complete"
            else (
                "credential-like values redacted"
                if redacted_any
                else "full thread conversation pages are not established"
            )
        ),
        truncated=status == "truncated",
    )


def _discover_ci_runs(
    snapshot: dict, api: Callable, deadline: float
) -> tuple[list[dict], bool, str | None]:
    target = _subject_target(snapshot)
    repo = target.split("#", 1)[0]
    head = snapshot.get("head")
    if not repo or not isinstance(head, str) or not SHA.fullmatch(head):
        return [], False, None
    collected: list[dict] = []
    complete, cursor = False, None
    for page in range(1, MAX_RUN_PAGES + 1):
        if time.monotonic() >= deadline:
            return collected, False, cursor
        suffix = f"?page={page}"
        doc = api(f"repos/{repo}/actions/runs?head_sha={head}&per_page=50{suffix}")
        rows = doc.get("workflow_runs", [])
        collected.extend(row for row in rows if row.get("head_sha") == head)
        if len(collected) > MAX_RUNS:
            return collected[:MAX_RUNS], False, cursor
        if len(rows) < 50:
            complete = True
            break
    return collected, complete, cursor


def _is_round_control_exception(exc: Exception) -> bool:
    """Keep typed scheduler deferrals visible to the pipeline's resource/deadline policy."""
    return isinstance(exc, RoundControlException)


def _artifact_section(
    snapshot: dict,
    api: Callable,
    deadline: float,
    root: Path,
    artifact_fetch=None,
    previous_section: dict | None = None,
    download_budget: dict | None = None,
) -> dict:
    try:
        runs, inventory_complete, cursor = _discover_ci_runs(snapshot, api, deadline)
    except Exception as exc:
        if _is_round_control_exception(exc):
            raise
        return _section("unknown", reason=f"CI run inventory failed: {type(exc).__name__}")
    if not runs:
        return _section(
            "pending" if not inventory_complete else "missing",
            {"runs": []},
            reason=(
                "current-head CI run inventory pending"
                if not inventory_complete
                else "no current-head CI runs found"
            ),
            provenance={"head": snapshot.get("head"), "pagination_complete": inventory_complete},
        )
    run_rows, artifacts_all, unavailable, any_pending, truncated = (
        [],
        [],
        [],
        not inventory_complete,
        not inventory_complete,
    )
    budget = (
        download_budget
        if download_budget is not None
        else {
            "archives_remaining": MAX_PACKET_ARCHIVES,
            "bytes_remaining": MAX_PACKET_ARCHIVE_BYTES,
        }
    )
    old_content = (previous_section or {}).get("content") or {}
    old_runs = {
        (str(row.get("id")), str(row.get("run_attempt"))): row
        for row in old_content.get("runs", [])
        if isinstance(row, dict)
    }
    old_artifacts = old_content.get("artifacts", [])
    unassociated_pr_identity: tuple[bool, str] | None = None

    def unique_unassociated_pr() -> tuple[bool, str]:
        """Bind an empty run association only to one exact current PR head."""
        nonlocal unassociated_pr_identity
        if unassociated_pr_identity is not None:
            return unassociated_pr_identity
        branch = snapshot.get("head_branch")
        head_repo = snapshot.get("head_repository")
        head = snapshot.get("head")
        if (
            not isinstance(branch, str)
            or not branch
            or not isinstance(head_repo, str)
            or not re.fullmatch(r"[^/#]+/[^/#]+", head_repo)
            or not isinstance(head, str)
            or not SHA.fullmatch(head)
        ):
            unassociated_pr_identity = (False, "linked PR head identity is incomplete")
            return unassociated_pr_identity
        owner = head_repo.split("/", 1)[0]
        endpoint = (
            f"repos/{subject_repo}/pulls?state=all&head={quote(owner + ':' + branch, safe='')}"
            "&per_page=100"
        )
        try:
            candidates = api(endpoint)
        except Exception as exc:
            if _is_round_control_exception(exc):
                raise
            unassociated_pr_identity = (
                False,
                f"linked PR head identity lookup failed: {type(exc).__name__}",
            )
            return unassociated_pr_identity
        if not isinstance(candidates, list) or len(candidates) >= 100:
            unassociated_pr_identity = (False, "linked PR head identity inventory is incomplete")
            return unassociated_pr_identity
        matching = []
        for row in candidates:
            if not isinstance(row, dict) or not isinstance(row.get("head"), dict):
                unassociated_pr_identity = (False, "linked PR head identity inventory is malformed")
                return unassociated_pr_identity
            candidate_head = row["head"]
            if candidate_head.get("sha") != head:
                continue
            candidate_repo = candidate_head.get("repo")
            if (
                not isinstance(candidate_head.get("ref"), str)
                or not isinstance(candidate_repo, dict)
                or not isinstance(candidate_repo.get("full_name"), str)
                or type(row.get("number")) is not int
                or row["number"] <= 0
            ):
                unassociated_pr_identity = (False, "linked PR head identity inventory is malformed")
                return unassociated_pr_identity
            if (
                candidate_head["ref"] == branch
                and candidate_repo["full_name"].lower() == head_repo.lower()
            ):
                matching.append(row)
        if len(matching) != 1 or matching[0].get("number") != int(subject_num):
            unassociated_pr_identity = (False, "linked PR head identity is ambiguous or mismatched")
        else:
            unassociated_pr_identity = (True, "")
        return unassociated_pr_identity

    for run_stub in runs:
        if time.monotonic() >= deadline:
            any_pending = True
            break
        run_id = run_stub.get("id")
        if not isinstance(run_id, int):
            continue
        subject = _subject_target(snapshot)
        subject_repo, subject_num = subject.split("#", 1)
        try:
            run = api(f"repos/{subject_repo}/actions/runs/{run_id}")
        except Exception as exc:
            if _is_round_control_exception(exc):
                raise
            any_pending = True
            unavailable.append(
                {
                    "run_id": run_id,
                    "status": "unknown",
                    "reason": f"run lookup failed: {type(exc).__name__}",
                }
            )
            continue
        pull_requests = run.get("pull_requests") if isinstance(run, dict) else None
        if not isinstance(run, dict) or not isinstance(pull_requests, list):
            any_pending = True
            unavailable.append(
                {
                    "run_id": run_id,
                    "status": "unknown",
                    "reason": "run detail or pull-request association is malformed",
                }
            )
            continue
        associated: list[int] = []
        malformed_association = False
        for row in pull_requests:
            number = row.get("number") if isinstance(row, dict) else None
            if type(number) is not int or number <= 0:
                malformed_association = True
                break
            associated.append(number)
        if malformed_association:
            any_pending = True
            unavailable.append(
                {
                    "run_id": run_id,
                    "status": "unknown",
                    "reason": "pull-request association contains a malformed number",
                }
            )
            continue
        if run.get("head_sha") != snapshot.get("head"):
            continue
        association_basis = "explicit_pull_requests"
        if int(subject_num) not in associated:
            if associated or run.get("event") != "pull_request":
                continue
            head_repo = run.get("head_repository")
            run_repo = run.get("repository")
            if (
                not isinstance(head_repo, dict)
                or not isinstance(run_repo, dict)
                or str(head_repo.get("full_name", "")).lower()
                != str(snapshot.get("head_repository", "")).lower()
                or str(run_repo.get("full_name", "")).lower() != subject_repo.lower()
                or not snapshot.get("head_repository")
                or run.get("head_branch") != snapshot.get("head_branch")
                or not snapshot.get("head_branch")
            ):
                reason = "unassociated PR run head branch or repository identity mismatches"
                valid_identity = False
            else:
                valid_identity, reason = unique_unassociated_pr()
            if not valid_identity:
                any_pending = True
                unavailable.append({"run_id": run_id, "status": "unknown", "reason": reason})
                continue
            association_basis = "unique_exact_pr_head"
        identity = {
            k: run.get(k)
            for k in (
                "id",
                "run_attempt",
                "head_sha",
                "event",
                "name",
                "status",
                "conclusion",
                "created_at",
                "updated_at",
            )
        }
        identity["association_basis"] = association_basis
        if association_basis == "unique_exact_pr_head":
            identity["head_branch"] = run["head_branch"]
            identity["head_repository"] = snapshot["head_repository"]
        run_rows.append(identity)
        if run.get("status") != "completed":
            any_pending = True
            unavailable.append(
                {
                    "run_id": run_id,
                    "run_attempt": run.get("run_attempt"),
                    "head_sha": run.get("head_sha"),
                    "status": "pending",
                    "reason": "current-head workflow run is not terminal",
                }
            )
        old_run = old_runs.get((str(run_id), str(run.get("run_attempt"))))
        old_rows = [
            item
            for item in old_artifacts
            if str(item.get("run_id")) == str(run_id)
            and str(item.get("run_attempt")) == str(run.get("run_attempt"))
        ]
        stable_run = all(
            old_run is not None and old_run.get(key) == identity.get(key)
            for key in ("id", "run_attempt", "head_sha", "event", "status", "conclusion")
        )
        cached_run = (
            stable_run
            and old_run is not None
            and run.get("status") == "completed"
            and bool(old_rows)
            and old_run.get("artifacts_inventory_complete") is True
            and all(
                item.get("status") == "complete"
                and _retained_ref_valid(item.get("archive"))
                and all(
                    _retained_ref_valid(receipt.get("raw"))
                    for receipt in item.get("receipts", [])
                    if isinstance(receipt, dict)
                )
                for item in old_rows
            )
        )
        # Refresh artifact inventory on every collection. Reuse retained archive bytes
        # only if the exact live artifact identity set still matches this completed run.
        try:
            art_doc = api(f"repos/{subject_repo}/actions/runs/{run_id}/artifacts?per_page=100")
            artifacts = art_doc.get("artifacts", [])
        except Exception as exc:
            if _is_round_control_exception(exc):
                raise
            artifacts = []
            unavailable.append(
                {
                    "run_id": run_id,
                    "run_attempt": run.get("run_attempt"),
                    "status": "unknown",
                    "reason": f"artifact inventory failed: {type(exc).__name__}",
                }
            )
            any_pending = True

        def artifact_key(item):
            return tuple(
                str(item.get(key))
                for key in ("id", "name", "size_in_bytes", "expired", "created_at")
            )

        inventory_same = (
            len(artifacts) < 100
            and len(artifacts) <= MAX_ARTIFACTS
            and len(old_rows) == len(artifacts)
            and sorted(artifact_key(item) for item in old_rows)
            == sorted(artifact_key(item) for item in artifacts)
        )
        cached_run = cached_run and inventory_same
        if cached_run:
            needed = sum(int(item.get("archive", {}).get("bytes", 0)) for item in old_rows)
            if (
                len(old_rows) <= budget["archives_remaining"]
                and needed <= budget["bytes_remaining"]
            ):
                identity["artifacts_inventory_complete"] = True
                artifacts_all.extend(old_rows)
                budget["archives_remaining"] -= len(old_rows)
                budget["bytes_remaining"] -= needed
                continue
            truncated = True
            unavailable.append(
                {
                    "run_id": run_id,
                    "run_attempt": run.get("run_attempt"),
                    "status": "truncated",
                    "reason": "packet-wide cached archive cap reached",
                }
            )
            continue

        if len(artifacts) >= 100 or len(artifacts) > MAX_ARTIFACTS:
            truncated = True
        identity["artifacts_inventory_complete"] = (
            len(artifacts) < 100 and len(artifacts) <= MAX_ARTIFACTS
        )
        for artifact in artifacts[:MAX_ARTIFACTS]:
            artifact_identity = {
                k: artifact.get(k) for k in ("id", "name", "size_in_bytes", "expired", "created_at")
            }
            artifact_identity.update(
                run_id=run_id, run_attempt=run.get("run_attempt"), head_sha=run.get("head_sha")
            )
            if artifact.get("expired") or artifact_fetch is None:
                missing = {
                    **artifact_identity,
                    "status": "missing",
                    "reason": "artifact content unavailable",
                }
                artifacts_all.append(missing)
                unavailable.append(missing)
                continue
            if time.monotonic() >= deadline:
                any_pending = True
                break
            if budget["archives_remaining"] <= 0 or budget["bytes_remaining"] <= 0:
                missing = {
                    **artifact_identity,
                    "status": "truncated",
                    "reason": "packet-wide archive cap reached",
                }
                artifacts_all.append(missing)
                unavailable.append(missing)
                truncated = True
                continue
            try:
                max_bytes = min(MAX_ARCHIVE_BYTES, budget["bytes_remaining"])
                archive = artifact_fetch(artifact, deadline=deadline, max_bytes=max_bytes)
                if not isinstance(archive, bytes) or len(archive) > max_bytes:
                    missing = {
                        **artifact_identity,
                        "status": "truncated",
                        "reason": "archive byte cap exceeded",
                    }
                    artifacts_all.append(missing)
                    unavailable.append(missing)
                    truncated = True
                    continue
                budget["archives_remaining"] -= 1
                budget["bytes_remaining"] -= len(archive)
                archive_ref = _retain_bytes(root, archive, "zip")
                parsed, omissions, archive_status = _parse_archive(archive, root)
                artifacts_all.append(
                    {
                        **artifact_identity,
                        "status": archive_status,
                        "archive": archive_ref,
                        "receipts": parsed,
                        "omissions": omissions,
                    }
                )
                truncated |= archive_status == "truncated"
                if archive_status != "complete":
                    unavailable.append(
                        {
                            **artifact_identity,
                            "status": archive_status,
                            "reason": "artifact was retrieved but yielded incomplete or unrecognized contents",
                        }
                    )
            except Exception as exc:
                if _is_round_control_exception(exc):
                    raise
                missing = {
                    **artifact_identity,
                    "status": "unknown",
                    "reason": f"fetch failed: {type(exc).__name__}",
                }
                artifacts_all.append(missing)
                unavailable.append(missing)
    receipt = any(a.get("receipts") for a in artifacts_all)
    retrieval_status = (
        "truncated"
        if truncated
        else (
            "partial"
            if (any_pending or unavailable) and (run_rows or artifacts_all)
            else "unknown" if any_pending or unavailable else "complete" if receipt else "missing"
        )
    )
    # A complete retrieval inventory does not establish that every required job/check ran.
    # That requires a separately configured required-check topology, which this producer lacks.
    proof_status = "partial" if receipt else "unknown"
    status = retrieval_status
    return _section(
        status,
        {
            "runs": run_rows,
            "artifacts": artifacts_all,
            "unavailable": unavailable,
            "retrieval_status": retrieval_status,
            "proof_status": proof_status,
        },
        provenance={
            "head": snapshot.get("head"),
            "run_inventory_complete": inventory_complete,
            "run_count": len(run_rows),
            "pagination_cursor": cursor,
        },
        reason="required-check topology is not known; retrieval cannot certify complete CI proof",
        truncated=truncated,
    )


def _parse_archive(archive: bytes, root: Path) -> tuple[list[dict], list[dict], str]:
    receipts, omissions = [], []
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            infos = zf.infolist()
            total = sum(info.file_size for info in infos)
            if len(infos) > MAX_FILES or total > MAX_UNCOMPRESSED_BYTES:
                return (
                    [],
                    [{"reason": "ZIP entry count or uncompressed size cap exceeded"}],
                    "truncated",
                )
            for info in infos:
                path = PurePosixPath(info.filename)
                if info.is_dir():
                    continue
                if path.is_absolute() or ".." in path.parts:
                    omissions.append(
                        {"path": info.filename[:300], "reason": "unsafe or directory entry omitted"}
                    )
                    continue
                # Raw receipt inputs share the archive's bounded expansion budget.
                # MAX_SECTION_BYTES bounds the compact packet, not retained CI files.
                with zf.open(info) as entry:
                    raw = entry.read(MAX_UNCOMPRESSED_BYTES + 1)
                if len(raw) != info.file_size or len(raw) > MAX_UNCOMPRESSED_BYTES:
                    omissions.append({"path": info.filename[:300], "reason": "entry size mismatch"})
                    continue
                suffix = path.suffix.lower()
                row: dict[str, Any] | None = None
                if suffix == ".xml":
                    try:
                        row = _junit_receipt(raw)
                    except (ET.ParseError, ValueError):
                        omissions.append(
                            {"path": info.filename[:300], "reason": "invalid or unsafe XML"}
                        )
                        continue
                    if row is None:
                        omissions.append(
                            {
                                "path": info.filename[:300],
                                "reason": "XML format is not recognized JUnit",
                            }
                        )
                        continue
                elif suffix == ".json":
                    try:
                        doc = json.loads(raw)
                    except (ValueError, UnicodeDecodeError):
                        omissions.append({"path": info.filename[:300], "reason": "invalid JSON"})
                        continue
                    row = _recognized_json(doc)
                    if row is None:
                        omissions.append(
                            {
                                "path": info.filename[:300],
                                "reason": "JSON format is not a recognized test or coverage receipt",
                            }
                        )
                        continue
                else:
                    omissions.append(
                        {
                            "path": info.filename[:300],
                            "reason": "file format is not a recognized test or coverage receipt",
                        }
                    )
                    continue
                if row is None:
                    continue
                ref = _retain_bytes(root, raw, suffix.lstrip("."))
                receipts.append({"path": info.filename[:300], **row, "raw": ref})
    except (zipfile.BadZipFile, OSError, RuntimeError):
        return [], [{"reason": "invalid or unreadable ZIP archive"}], "unknown"
    status = (
        "partial"
        if omissions and receipts
        else "truncated" if omissions else "complete" if receipts else "missing"
    )
    return receipts, omissions[:MAX_FILES], status


def _junit_receipt(raw: bytes) -> dict | None:
    # Reject DTD/entity declarations before parsing, including UTF-16/32 forms.
    # JUnit output is data; custom entities are not part of its receipt contract.
    markup = raw.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in markup or b"<!ENTITY" in markup:
        raise ValueError("XML declarations are not receipt evidence")
    counts: dict[str, Any] = {"kind": "junit", "tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    stack: list[ET.Element] = []
    for event, element in ET.iterparse(io.BytesIO(raw), events=("start", "end")):
        tag = element.tag.split("}")[-1]
        if event == "start":
            if not stack and tag not in {"testsuites", "testsuite"}:
                return None
            stack.append(element)
            continue
        if tag == "testcase":
            counts["tests"] += 1
            children = {child.tag.split("}")[-1] for child in element}
            for key, child in (
                ("failures", "failure"),
                ("errors", "error"),
                ("skipped", "skipped"),
            ):
                counts[key] += child in children
        # Keep direct testcase children until its counters have been observed.
        if len(stack) < 2 or stack[-2].tag.split("}")[-1] != "testcase":
            element.clear()
            if len(stack) > 1:
                stack[-2].remove(element)
        stack.pop()
    return counts


def _recognized_json(doc: Any) -> dict | None:
    if not isinstance(doc, dict):
        return None

    def number(value):
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value >= 0
            and value == value
            and value != float("inf")
        )

    # Recognize only established summary shapes; arbitrary JSON is not test evidence.
    if all(number(doc.get(key)) for key in ("total", "passed", "failed")):
        skipped = doc.get("skipped", 0)
        if (
            not number(skipped)
            or doc["total"] <= 0
            or doc["passed"] <= 0
            or doc["total"] != doc["passed"] + doc["failed"] + skipped
        ):
            return None
        return {
            "kind": "test-summary",
            "total": doc["total"],
            "passed": doc["passed"],
            "failed": doc["failed"],
            "skipped": skipped,
        }
    totals = doc.get("totals")
    meta = doc.get("meta")
    if (
        isinstance(totals, dict)
        and isinstance(meta, dict)
        and meta.get("format") == 3
        and isinstance(meta.get("version"), str)
        and all(key in totals for key in ("num_statements", "covered_lines", "missing_lines"))
    ):

        def counter(value):
            return isinstance(value, int) and not isinstance(value, bool) and value >= 0

        count, covered, missing = (
            totals[key] for key in ("num_statements", "covered_lines", "missing_lines")
        )
        if (
            not all(counter(value) for value in (count, covered, missing))
            or covered + missing != count
        ):
            return None
        row = {
            "kind": "coverage-summary",
            "count": count,
            "covered": covered,
            "percent_covered": (100 * covered / count) if count else None,
            "source_format": "coverage.py-3",
        }
        branch_keys = ("num_branches", "covered_branches", "missing_branches")
        if any(key in totals for key in branch_keys):
            branches: list[Any] = [totals.get(key) for key in branch_keys]
            if (
                not all(counter(value) for value in branches)
                or branches[1] + branches[2] != branches[0]
            ):
                return None
            row["branches"] = {"count": branches[0], "covered": branches[1], "missing": branches[2]}
        # coverage.py's percent_covered mixes line and branch denominators; do not
        # relabel it as line coverage or let contradictory/nonfinite input pass.
        percent = totals.get("percent_covered")
        if percent is not None and (not number(percent) or percent > 100):
            return None
        return row
    if isinstance(doc.get("totals"), dict) and isinstance(doc["totals"].get("lines"), dict):
        lines = doc["totals"]["lines"]
        if all(number(lines.get(k)) for k in ("count", "covered")):
            percent = lines.get("percent_covered")
            if lines["covered"] > lines["count"] or (
                percent is not None and (not number(percent) or percent > 100)
            ):
                return None
            return {
                "kind": "coverage-summary",
                "count": lines["count"],
                "covered": lines["covered"],
                "percent_covered": percent,
            }
    return None


class NativeInputCaptureError(ValueError):
    """A required sealed transport component needs bounded, recoverable acquisition."""


def _native_manifest_presentation(capture: dict) -> dict:
    """Keep full authenticated bytes in raw storage, not a model section."""
    manifest = capture["manifest"]
    size = len(_bytes(manifest))
    if size <= MAX_TEXT_BYTES:
        return capture
    fields = (
        "schema",
        "capture_kind",
        "repository",
        "pr",
        "head_sha",
        "merge_sha",
        "source_run_id",
        "source_run_attempt",
        "context_sha256",
        "diff_sha256",
        "diff_summary_sha256",
        "mode",
        "requested_provider",
        "input_limits",
    )
    projected = {
        k: manifest[k] for k in fields if k in manifest and len(_bytes(manifest[k])) <= 8000
    }
    return {
        **capture,
        "manifest": projected,
        "manifest_projection": {
            "kind": "identity-and-input-digests",
            "full_content_bytes": capture["raw"]["bytes"],
            "supplied_fields": sorted(projected),
            "omitted_field_count": len(manifest) - len(projected),
            "full_content_ref": capture["raw"],
            "acceptance_authority": False,
        },
    }


def native_capture_artifact_names(run_id: str | int) -> tuple[str, str]:
    """Exact producer capture families; names grant no identity/verdict authority."""
    return (f"comparison-results-{run_id}", f"evaluation-results-{run_id}")


def _native_manifest_from_archive(archive: bytes, root: Path, *, retain=None) -> list[dict]:
    """Read native manifests and exact known verifier report files from an archive."""
    supplied_retain = retain
    if supplied_retain is None:
        retain = _retain_bytes
    else:

        def retain(root, data, suffix):
            try:
                return supplied_retain(root, data, suffix)
            except OSError as exc:
                from closer_attribution_store import AttributionStorageWait

                # A supplied retention sink failing is not a malformed ZIP.
                # Preserve a typed storage cause without leaking exception paths.
                raise AttributionStorageWait(
                    "attribution retention I/O unavailable",
                    accounting={"error_type": type(exc).__name__, "errno": exc.errno},
                ) from exc

    manifests = []
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise NativeInputCaptureError("native input transport limit: compressed archive")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            infos = zf.infolist()
            if (
                len(infos) > MAX_FILES
                or sum(item.file_size for item in infos) > MAX_UNCOMPRESSED_BYTES
            ):
                raise NativeInputCaptureError(
                    "native input transport limit: archive entries or expansion"
                )
            files = {}
            manifest_candidates = []
            native_names = set()
            actual_total = 0

            def read_entry(info, limit):
                nonlocal actual_total
                with zf.open(info) as stream:
                    raw = stream.read(limit + 1)
                if len(raw) > limit or len(raw) != info.file_size:
                    raise NativeInputCaptureError(
                        "native input transport limit or size mismatch: "
                        + PurePosixPath(info.filename).name[:100]
                    )
                actual_total += len(raw)
                if actual_total > MAX_UNCOMPRESSED_BYTES:
                    raise NativeInputCaptureError("native input transport limit: actual expansion")
                return raw

            for info in infos:
                path = PurePosixPath(info.filename)
                if path.name in NATIVE_INPUT_COMPONENTS and not info.is_dir():
                    # An unsafe competing path cannot shadow a canonical identity input.
                    if path.name in native_names:
                        raise NativeInputCaptureError(
                            "native input component ambiguous: " + path.name
                        )
                    native_names.add(path.name)
                    if path.is_absolute() or ".." in path.parts:
                        raise NativeInputCaptureError(
                            "native input canonical path invalid: " + path.name
                        )
                if info.is_dir() or path.is_absolute() or ".." in path.parts:
                    continue
                canonical = path.name in NATIVE_INPUT_COMPONENTS
                limit = MAX_NATIVE_INPUT_BYTES if canonical else MAX_SECTION_BYTES
                if info.file_size > limit:
                    if canonical:
                        raise NativeInputCaptureError("native input transport limit: " + path.name)
                    continue
                if path.suffix.lower() != ".json":
                    if path.name in {
                        "comparison-comment.md",
                        "evaluation-comment.md",
                        "verifier-diff-summary.md",
                        "verifier-context.md",
                        "verifier-pr-diff.patch",
                    }:
                        files[path.name] = read_entry(info, limit)
                    continue
                raw = read_entry(info, limit)
                files[path.name] = raw
                try:
                    doc = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if (
                    isinstance(doc, dict)
                    and doc.get("schema") == "workflows-verifier-input-snapshot/v1"
                ):
                    manifest_candidates.append((info.filename[:300], doc, raw))
            for path_name, doc, raw in manifest_candidates:
                row: dict[str, Any] = {
                    "path": path_name,
                    "manifest": doc,
                    "raw": retain(root, raw, "json"),
                    "receipts": [],
                    "omissions": [],
                }
                report_names = [
                    name for name in ("comparison.json", "evaluation.json") if name in files
                ]
                report_name = (
                    report_names[0]
                    if len(report_names) == 1
                    else "evaluation.json" if doc.get("mode") == "evaluate" else "comparison.json"
                )
                report = files.get(report_name) if len(report_names) == 1 else None
                evaluation_report = report_name == "evaluation.json"
                if report is not None:
                    try:
                        report_doc = json.loads(report)
                    except (ValueError, UnicodeDecodeError):
                        row["omissions"].append({"path": report_name, "reason": "invalid JSON"})
                    else:
                        report_ref = retain(root, report, "json")
                        findings = []
                        provider_results = []
                        raw_results = (
                            [report_doc]
                            if evaluation_report and isinstance(report_doc, dict)
                            else report_doc.get("results") if isinstance(report_doc, dict) else None
                        )
                        if evaluation_report and not isinstance(report_doc, dict):
                            row["omissions"].append(
                                {
                                    "path": report_name,
                                    "reason": "evaluation result is not an object",
                                }
                            )
                        if isinstance(raw_results, list):
                            if len(raw_results) > MAX_NATIVE_RESULTS:
                                row["omissions"].append(
                                    {
                                        "path": report_name,
                                        "reason": "provider result count cap reached",
                                    }
                                )
                            concern_count = 0
                            concern_cap_reached = False
                            for result_index, result in enumerate(raw_results[:MAX_NATIVE_RESULTS]):
                                if not isinstance(result, dict):
                                    row["omissions"].append(
                                        {
                                            "path": report_name,
                                            "reason": "provider result entry is not an object",
                                        }
                                    )
                                    continue
                                summary, summary_cut = _safe_text(result.get("summary"), 8000)
                                provider = {
                                    "provider_index": result_index,
                                    "provider_used": str(result.get("provider_used") or "")[:200],
                                    "model": str(result.get("model") or "")[:200],
                                    "verdict": str(result.get("verdict") or "")[:100],
                                    "summary": summary,
                                    "text_is_untrusted": True,
                                }
                                if evaluation_report:
                                    provider["used_llm"] = result.get("used_llm")
                                    provider["error"] = _safe_text(result.get("error"), 8000)[0]
                                provider_results.append(provider)
                                if summary_cut:
                                    row["omissions"].append(
                                        {
                                            "path": report_name,
                                            "reason": "provider summary text truncated",
                                        }
                                    )
                                concerns = result.get("concerns")
                                if not isinstance(concerns, list):
                                    row["omissions"].append(
                                        {
                                            "path": report_name,
                                            "reason": "provider concern list missing or invalid",
                                        }
                                    )
                                    continue
                                if len(concerns) > 100:
                                    row["omissions"].append(
                                        {
                                            "path": report_name,
                                            "reason": "per-provider concern count cap reached",
                                        }
                                    )
                                for concern_index, concern in enumerate(concerns[:100]):
                                    if concern_count >= MAX_NATIVE_CONCERNS:
                                        if not concern_cap_reached:
                                            row["omissions"].append(
                                                {
                                                    "path": report_name,
                                                    "reason": "aggregate concern count cap reached",
                                                }
                                            )
                                            concern_cap_reached = True
                                        break
                                    if isinstance(concern, dict):
                                        value = concern.get("text") or concern.get("concern")
                                        text = (
                                            str(value)
                                            if value is not None
                                            else _bytes(concern).decode("utf-8", "replace")
                                        )
                                    else:
                                        text = str(concern)
                                    text, text_cut = _safe_text(text, 8000)
                                    if text_cut:
                                        row["omissions"].append(
                                            {
                                                "path": report_name,
                                                "reason": "provider concern text truncated",
                                            }
                                        )
                                    if text:
                                        concern_count += 1
                                        findings.append(
                                            {
                                                "id": f"result-{result_index}-concern-{concern_index}",
                                                **provider,
                                                "text": text,
                                            }
                                        )
                                    else:
                                        row["omissions"].append(
                                            {
                                                "path": report_name,
                                                "reason": "provider concern has no text",
                                            }
                                        )
                                if concern_cap_reached:
                                    break
                        elif isinstance(report_doc, dict) and isinstance(
                            report_doc.get("findings"), list
                        ):
                            for item in report_doc["findings"][:100]:
                                if isinstance(item, dict):
                                    findings.append(
                                        {
                                            "id": str(
                                                item.get("id") or item.get("finding_id") or ""
                                            )[:200],
                                            "text": str(
                                                item.get("finding")
                                                or item.get("text")
                                                or item.get("summary")
                                                or ""
                                            )[:8000],
                                            "text_is_untrusted": True,
                                        }
                                    )
                        row["receipts"].append(
                            {
                                "kind": (
                                    "native-evaluation"
                                    if evaluation_report
                                    else "native-comparison"
                                ),
                                "path": report_name,
                                **(
                                    {
                                        "acceptance_authority": False,
                                        "resolution_authority": False,
                                        "text_is_untrusted": True,
                                    }
                                    if evaluation_report
                                    else {}
                                ),
                                "results_count": (
                                    len(raw_results) if isinstance(raw_results, list) else None
                                ),
                                "aggregate_report_excerpt": _safe_text(
                                    (
                                        report_doc.get("summary" if evaluation_report else "report")
                                        if isinstance(report_doc, dict)
                                        else ""
                                    ),
                                    8000,
                                )[0],
                                "findings": findings,
                                "provider_results": provider_results,
                                "raw": report_ref,
                            }
                        )
                else:
                    row["omissions"].append(
                        {
                            "path": (
                                "comparison.json/evaluation.json"
                                if len(report_names) > 1
                                else report_name
                            ),
                            "reason": (
                                "native reports ambiguous"
                                if len(report_names) > 1
                                else f"native {'evaluation' if evaluation_report else 'comparison'} report missing"
                            ),
                        }
                    )
                known_files = (
                    ("comparison-comment.md", "native-comparison-comment", None, False),
                    ("evaluation-comment.md", "native-evaluation-comment", None, False),
                    ("evaluation-execution.json", "native-evaluation-execution", None, False),
                    ("closer-correction-seeds.json", "native-correction-seeds", None, False),
                    (
                        "verifier-diff-summary.md",
                        "native-verifier-diff-summary",
                        "diff_summary_sha256",
                        True,
                    ),
                    ("verifier-context.md", "native-verifier-context", "context_sha256", True),
                    ("verifier-pr-diff.patch", "native-pr-diff", "diff_sha256", True),
                )
                for filename, kind, hash_key, missing_is_omission in known_files:
                    content = files.get(filename)
                    if content is None:
                        if missing_is_omission and (
                            hash_key is None or doc.get(hash_key) is not None
                        ):
                            row["omissions"].append(
                                {"path": filename, "reason": "native report file missing"}
                            )
                        continue
                    actual = hashlib.sha256(content).hexdigest()
                    expected = doc.get(hash_key) if hash_key else None
                    if expected is not None and expected != actual:
                        row["omissions"].append(
                            {"path": filename, "reason": f"{hash_key} mismatch"}
                        )
                        continue
                    receipt: dict[str, Any] = {
                        "kind": kind,
                        "path": filename,
                        "manifest_hash_verified": expected is not None,
                        "raw": retain(
                            root, content, "patch" if filename.endswith(".patch") else "txt"
                        ),
                        "sha256": actual,
                    }
                    if kind.startswith("native-evaluation"):
                        receipt.update(
                            acceptance_authority=False,
                            resolution_authority=False,
                            text_is_untrusted=True,
                        )
                    if filename.endswith(".md"):
                        receipt["excerpt"] = content[:8000].decode("utf-8", "replace")
                        receipt["text_is_untrusted"] = True
                    row["receipts"].append(receipt)
                manifests.append(row)
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        from closer_attribution_store import AttributionStorageWait

        if isinstance(exc, AttributionStorageWait):
            raise
        return []
    return manifests


def _native_diagnostic_section(
    snapshot,
    api,
    deadline,
    root,
    artifact_fetch,
    download_budget,
    *,
    known_run=None,
    semantic_reference=None,
):
    """Retain existing exact-delivery corpus without inventing a trusted verdict."""
    target = _subject_target(snapshot)
    repo, _, number = target.partition("#")
    head, merge = snapshot.get("head"), snapshot.get("merge")
    import closer_verifier_boundary as boundary

    semantic_bound = (
        isinstance(semantic_reference, dict)
        and isinstance(known_run, dict)
        and semantic_reference.get("subject_pr") == target
        and semantic_reference.get("head") == head
        and semantic_reference.get("merge") == merge
        and isinstance(semantic_reference.get("archive_digest"), str)
        and re.fullmatch("[0-9a-f]{64}", semantic_reference["archive_digest"]) is not None
        and boundary.logical_run(known_run)
        == boundary.logical_run(semantic_reference.get("run", {}))
    )
    content: dict[str, Any] = {
        "evidence_type": "native-verifier-diagnostic",
        "acceptance_authority": False,
        "resolution_authority": False,
        "runs": [],
        "artifacts": [],
        "unavailable": [],
    }
    reason = "trusted native verdict missing or unsupported; retained corpus is diagnostic only"
    if (
        not isinstance(head, str)
        or not SHA.fullmatch(head)
        or not isinstance(merge, str)
        or not SHA.fullmatch(merge)
        or artifact_fetch is None
    ):
        return _section("missing", content, reason=reason)
    budget = (
        download_budget
        if download_budget is not None
        else {
            "archives_remaining": MAX_PACKET_ARCHIVES,
            "bytes_remaining": MAX_PACKET_ARCHIVE_BYTES,
        }
    )
    try:
        if time.monotonic() >= deadline:
            return _section(
                "pending", content, reason="native diagnostic collection deadline reached"
            )
        doc = (
            {"workflow_runs": [known_run], "total_count": 1}
            if known_run is not None
            else api(
                f"repos/{repo}/actions/workflows/agents-verifier.yml/runs?head_sha={head}&per_page=100"
            )
        )
        rows = doc.get("workflow_runs")
        if not isinstance(rows, list):
            return _section("unknown", content, reason="native diagnostic run inventory malformed")
        candidates = [
            row
            for row in rows
            if isinstance(row, dict) and (semantic_bound or row.get("head_sha") == head)
        ]
        if len(candidates) > 3 or doc.get("total_count", len(rows)) > len(rows):
            return _section(
                "unknown", content, reason="native diagnostic run inventory bound exceeded"
            )
        for candidate in candidates:
            run_id = str(candidate.get("id", ""))
            if not run_id.isdigit() or time.monotonic() >= deadline:
                content["unavailable"].append(
                    {"run_id": run_id, "reason": "run identity or deadline unavailable"}
                )
                break
            run = known_run if known_run is not None else api(f"repos/{repo}/actions/runs/{run_id}")
            actual_repo = str((run.get("repository") or {}).get("full_name") or "")
            if (
                str(run.get("id")) != run_id
                or (not semantic_bound and run.get("head_sha") != head)
                or not isinstance(run.get("head_sha"), str)
                or not SHA.fullmatch(run["head_sha"])
                or actual_repo.lower() != repo.lower()
                or run.get("path") != ".github/workflows/agents-verifier.yml"
                or run.get("event") not in {"workflow_dispatch", "pull_request_target"}
                or run.get("status") != "completed"
                or type(run.get("run_attempt")) is not int
                or run["run_attempt"] < 1
            ):
                content["unavailable"].append(
                    {"run_id": run_id, "reason": "native diagnostic run identity mismatch"}
                )
                continue
            content["runs"].append(
                {
                    key: run.get(key)
                    for key in (
                        "id",
                        "run_attempt",
                        "head_sha",
                        "status",
                        "conclusion",
                        "event",
                        "path",
                    )
                }
            )
            if time.monotonic() >= deadline:
                break
            inventory = api(f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100")
            artifacts = inventory.get("artifacts", [])
            if not isinstance(artifacts, list) or inventory.get(
                "total_count", len(artifacts)
            ) > len(artifacts):
                content["unavailable"].append(
                    {"run_id": run_id, "reason": "native diagnostic artifact inventory incomplete"}
                )
                continue
            matches = [
                a
                for a in artifacts
                if isinstance(a, dict) and a.get("name") in native_capture_artifact_names(run_id)
            ]
            if len(matches) != 1:
                content["unavailable"].append(
                    {"run_id": run_id, "reason": "native comparison corpus missing or ambiguous"}
                )
                continue
            artifact = matches[0]
            if (
                (artifact.get("expired") and not semantic_bound)
                or time.monotonic() >= deadline
                or budget["archives_remaining"] <= 0
                or budget["bytes_remaining"] <= 0
            ):
                content["unavailable"].append(
                    {
                        "run_id": run_id,
                        "artifact_id": artifact.get("id"),
                        "reason": "native corpus expired or original collection bound exhausted",
                    }
                )
                continue
            limit = min(MAX_ARCHIVE_BYTES, budget["bytes_remaining"])
            archive = artifact_fetch(artifact, deadline=deadline, max_bytes=limit)
            if not isinstance(archive, bytes) or len(archive) > limit:
                raise ValueError("native diagnostic archive byte cap exceeded")
            budget["archives_remaining"] -= 1
            budget["bytes_remaining"] -= len(archive)
            if (
                semantic_bound
                and hashlib.sha256(archive).hexdigest() != semantic_reference["archive_digest"]
            ):
                content["unavailable"].append(
                    {"run_id": run_id, "reason": "semantic archive digest changed"}
                )
                continue
            archive_ref = _retain_bytes(root, archive, "zip")
            parsed = _native_manifest_from_archive(archive, root)
            bound = [
                row
                for row in parsed
                if (
                    str(row["manifest"].get("repository", "")).lower() == repo.lower()
                    and str(row["manifest"].get("pr")) == number
                    and row["manifest"].get("merge_sha") == merge
                    and str(row["manifest"].get("source_run_id")) == run_id
                )
            ]
            content["artifacts"].append(
                {
                    "id": artifact.get("id"),
                    "run_id": int(run_id),
                    "observed_run_attempt": run["run_attempt"],
                    "artifact_run_attempt": None,
                    "artifact_attempt_status": "unknown",
                    "head_sha": run["head_sha"],
                    "evaluated_sha": merge,
                    "archive": archive_ref,
                    "identity_bound": len(bound) == 1,
                    "input_snapshots": [_native_manifest_presentation(row) for row in bound],
                }
            )
    except Exception as exc:
        if _is_round_control_exception(exc):
            raise
        content["unavailable"].append(
            {
                "reason": (
                    str(exc)
                    if isinstance(exc, NativeInputCaptureError)
                    else "native diagnostic lookup failed"
                ),
                "error_type": type(exc).__name__,
            }
        )
    return _section(
        "unknown",
        content,
        reason=reason,
        provenance={
            "repository": repo,
            "pr": number,
            "head_sha": head,
            "evaluated_sha": merge,
            "acceptance_proof": False,
        },
    )


def _native_verifier_section(
    snapshot: dict,
    api: Callable,
    deadline: float,
    root: Path,
    artifact_fetch=None,
    download_budget: dict | None = None,
    *,
    native_archive_root=None,
) -> dict:
    """Collect exact merge-bound verifier input only for a trusted NON_PASS marker."""
    marker = snapshot.get("verifier")
    target = _subject_target(snapshot)
    repo, _, number = target.partition("#")
    merge, head = snapshot.get("merge"), snapshot.get("head")
    reference = snapshot.get("native_diagnostic_reference")
    if isinstance(reference, dict) and not marker:
        import closer_verifier_boundary as boundary

        try:
            expected = reference["run"]
            run = api(f"repos/{repo}/actions/runs/{expected['id']}")
            if (
                reference.get("subject_pr") != target
                or reference.get("head") != head
                or reference.get("merge") != merge
                or boundary.logical_run(run) != boundary.logical_run(expected)
            ):
                return _section(
                    "unknown", reason="native diagnostic reference changed before collection"
                )
            reads: list[dict[str, Any]] = []
            relation, requirement = boundary.attribute(
                run,
                snapshot,
                api,
                artifact_fetch,
                Path(native_archive_root) if native_archive_root is not None else root,
                deadline=deadline,
                reads=reads,
            )
            archive_read: dict[str, Any] = next(
                (r for r in reversed(reads) if r["endpoint"].startswith("archive:")), {}
            )
            if relation != "same" or not archive_read.get("endpoint", "").endswith(
                "sha256:" + str(reference.get("archive_digest"))
            ):
                return _section(
                    "unknown",
                    {"retained_reads": reads},
                    reason=requirement or "native diagnostic archive changed",
                )
            return _native_diagnostic_section(
                snapshot,
                api,
                deadline,
                root,
                artifact_fetch,
                download_budget,
                known_run=run,
                semantic_reference=reference,
            )
        except Exception as exc:
            if _is_round_control_exception(exc):
                raise
            return _section(
                "unknown", reason=f"native diagnostic reference unavailable: {type(exc).__name__}"
            )
    if (
        not isinstance(marker, dict)
        or marker.get("schema") != "verifier-corpus-decision/v1"
        or marker.get("verdict") != "NON_PASS"
        or str(marker.get("repo", "")).lower() != repo.lower()
        or str(marker.get("pr")) != number
        or marker.get("head_sha") != head
        or not isinstance(merge, str)
        or not SHA.fullmatch(merge)
        or marker.get("evaluated_sha") != merge
    ):
        if snapshot.get("state") == "MERGED" and (
            "verify:compare" in snapshot.get("labels", [])
            or snapshot.get("verifier_run_identity_unknown")
        ):
            return _native_diagnostic_section(
                snapshot, api, deadline, root, artifact_fetch, download_budget
            )
        return _section(
            "missing", reason="no trusted exact-identity NON_PASS marker for current merged PR"
        )
    run_id, attempt = str(marker.get("run_id", "")), str(marker.get("run_attempt", ""))
    if not run_id.isdigit() or not attempt.isdigit() or int(run_id) < 1 or int(attempt) < 1:
        return _section("unknown", reason="trusted marker lacks valid native verifier run identity")
    if artifact_fetch is None:
        return _section(
            "pending",
            {"verifier": marker},
            reason="native verifier artifact downloader unavailable",
        )
    try:
        if time.monotonic() >= deadline:
            return _section("pending", {"verifier": marker}, reason="collection deadline reached")
        run = api(f"repos/{repo}/actions/runs/{run_id}")
        actual_repo = str(
            (run.get("repository") or {}).get("full_name")
            or run.get("head_repository", {}).get("full_name")
            or ""
        )
        if (
            str(run.get("id")) != run_id
            or str(run.get("run_attempt")) != attempt
            or str(run.get("event")) not in {"workflow_dispatch", "pull_request_target"}
            or str(run.get("path")) != ".github/workflows/agents-verifier.yml"
            or run.get("status") != "completed"
            or actual_repo.lower() != repo.lower()
        ):
            diagnostic = _native_diagnostic_section(
                snapshot, api, deadline, root, artifact_fetch, download_budget, known_run=run
            )
            if isinstance(diagnostic.get("content"), dict):
                diagnostic["content"]["trusted_marker_observation"] = marker
                diagnostic["content"]["trusted_marker_mismatch"] = True
                diagnostic["reason"] = (
                    "native run repository/workflow/event/attempt identity mismatch; retained corpus is diagnostic only"
                )
                diagnostic["sha256"] = _section_hash(diagnostic)
            return diagnostic
        if time.monotonic() >= deadline:
            return _section("pending", {"verifier": marker}, reason="collection deadline reached")
        artifacts_doc = api(f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100")
        inventory = artifacts_doc.get("artifacts", [])
        artifacts: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        manifests: list[dict[str, Any]] = []
        budget = (
            download_budget
            if download_budget is not None
            else {
                "archives_remaining": MAX_PACKET_ARCHIVES,
                "bytes_remaining": MAX_PACKET_ARCHIVE_BYTES,
            }
        )
        for artifact in inventory[:MAX_ARTIFACTS]:
            ident = {
                "id": artifact.get("id"),
                "name": artifact.get("name"),
                "run_id": int(run_id),
                "run_attempt": int(attempt),
                "evaluated_sha": merge,
            }
            if artifact.get("expired") or time.monotonic() >= deadline:
                entry = {
                    **ident,
                    "status": "missing",
                    "reason": "artifact expired or collection deadline reached",
                }
                artifacts.append(entry)
                missing.append(entry)
                continue
            if budget["archives_remaining"] <= 0 or budget["bytes_remaining"] <= 0:
                entry = {
                    **ident,
                    "status": "truncated",
                    "reason": "packet-wide archive cap reached",
                }
                artifacts.append(entry)
                missing.append(entry)
                continue
            try:
                max_bytes = min(MAX_ARCHIVE_BYTES, budget["bytes_remaining"])
                archive = artifact_fetch(artifact, deadline=deadline, max_bytes=max_bytes)
                if not isinstance(archive, bytes) or len(archive) > max_bytes:
                    entry = {**ident, "status": "truncated", "reason": "archive byte cap exceeded"}
                    artifacts.append(entry)
                    missing.append(entry)
                    continue
                budget["archives_remaining"] -= 1
                budget["bytes_remaining"] -= len(archive)
                archive_ref = _retain_bytes(root, archive, "zip")
                found = _native_manifest_from_archive(archive, root)
                receipts = []
                for item in found:
                    presented = _native_manifest_presentation(item)
                    receipts.append(
                        {
                            "kind": "native-input-manifest",
                            "path": item["path"],
                            "manifest": presented["manifest"],
                            "raw": item["raw"],
                            **(
                                {"manifest_projection": presented["manifest_projection"]}
                                if "manifest_projection" in presented
                                else {}
                            ),
                        }
                    )
                    receipts.extend(item.get("receipts", []))
                    for receipt in receipts:
                        receipt.setdefault("run_id", int(run_id))
                        receipt.setdefault("run_attempt", int(attempt))
                        receipt.setdefault("evaluated_sha", merge)
                entry = {
                    **ident,
                    "status": (
                        "complete"
                        if found and not any(item.get("omissions") for item in found)
                        else "partial" if found else "missing"
                    ),
                    "archive": archive_ref,
                    "receipts": receipts,
                    "omissions": [om for item in found for om in item.get("omissions", [])],
                }
                artifacts.append(entry)
                manifests.extend(found)
                for item in found:
                    if item.get("omissions"):
                        missing.extend({**ident, **omission} for omission in item["omissions"])
                if not found:
                    missing.append({**ident, "reason": "no native input snapshot in artifact"})
            except Exception as exc:
                if _is_round_control_exception(exc):
                    raise
                entry = {
                    **ident,
                    "status": "unknown",
                    "reason": (
                        str(exc)
                        if isinstance(exc, NativeInputCaptureError)
                        else f"artifact fetch failed: {type(exc).__name__}"
                    ),
                }
                artifacts.append(entry)
                missing.append(entry)
        if len(inventory) > MAX_ARTIFACTS or len(inventory) >= 100:
            missing.append(
                {"status": "truncated", "reason": "native artifact inventory cap reached"}
            )
        if len(manifests) != 1:
            return _section(
                "unknown" if not manifests else "partial",
                {
                    "verifier": marker,
                    "run": {
                        k: run.get(k)
                        for k in ("id", "run_attempt", "status", "conclusion", "head_sha", "event")
                    },
                    "artifacts": artifacts,
                    "unavailable": missing,
                },
                reason="native input manifest missing or ambiguous",
            )
        manifest = manifests[0]["manifest"]
        source_id = str(manifest.get("source_run_id", ""))
        source_ok = source_id == run_id
        manifest_ok = (
            str(manifest.get("repository", "")).lower() == repo.lower()
            and str(manifest.get("pr")) == number
            and manifest.get("merge_sha") == merge
            and source_ok
        )
        manifest_ref = manifests[0]["raw"]
        native_run = {
            k: run.get(k)
            for k in ("id", "run_attempt", "status", "conclusion", "head_sha", "event", "path")
        }
        presented = _native_manifest_presentation(manifests[0])
        content = {
            "verifier": marker,
            "run": native_run,
            "artifacts": artifacts,
            "input_manifest": {**presented["manifest"], "raw": manifest_ref},
            **(
                {"input_manifest_projection": presented["manifest_projection"]}
                if "manifest_projection" in presented
                else {}
            ),
            "unavailable": missing,
            "evidence_type": "evaluated-merge-verifier-non-pass",
            "proof_status": "resolution-only",
        }
        if not manifest_ok:
            return _section(
                "unknown",
                content,
                reason="native manifest repository/PR/merge/source-run binding mismatch",
            )
        return _section(
            "partial" if missing else "complete",
            content,
            provenance={
                "repository": repo,
                "pr": number,
                "head_sha": head,
                "evaluated_sha": merge,
                "run_id": int(run_id),
                "run_attempt": int(attempt),
                "source_run_id": source_id,
                "trigger_event": run.get("event"),
                "native_manual_run": run.get("event") == "workflow_dispatch",
                "native_labeled_pr_run": run.get("event") == "pull_request_target",
                "acceptance_proof": False,
            },
            reason="native verifier NON_PASS evidence is resolution-only, never acceptance proof",
        )
    except Exception as exc:
        if _is_round_control_exception(exc):
            raise
        return _section(
            "unknown",
            {"verifier": marker},
            reason=f"native verifier evidence lookup failed: {type(exc).__name__}",
        )


def collect_evidence(
    snapshot: dict,
    api: Callable,
    retained_dir: str | Path,
    *,
    previous: dict | None = None,
    deadline: float | None = None,
    dispositions: dict | None = None,
    artifact_fetch=None,
    review_threads_fetch=None,
    action: str | None = None,
    refresh_sections: set[str] | None = None,
    supplemental_sections: dict | None = None,
    native_archive_root: str | Path | None = None,
) -> dict:
    """Create an incremental evidence packet.

    `api(endpoint, *, method='GET', fields=None, resource='core')` is GitHub.api-shaped.
    Optional `artifact_fetch(artifact, *, deadline, max_bytes)->bytes` downloads existing
    archive contents. Optional `review_threads_fetch(snapshot, *, deadline, max_pages,
    max_threads, max_comments_per_thread)->{threads, complete, has_next_page, end_cursor}`
    obtains all review comment pages. Missing callbacks lower evidence status explicitly.
    """
    started = time.monotonic()
    deadline = deadline if deadline is not None else started + 20
    root = Path(retained_dir)
    target, subject_target, head, base = (
        str(snapshot.get("target") or ""),
        _subject_target(snapshot),
        snapshot.get("head"),
        snapshot.get("base"),
    )
    subject_pr = subject_target if snapshot.get("kind") == "pr" else None
    sources = snapshot.get("sources") or []
    producer_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    prior_packet = previous if isinstance(previous, dict) and validate_packet(previous) else None
    prior_ok = bool(
        prior_packet is not None
        and prior_packet.get("producer_sha256") == producer_sha
        and prior_packet.get("target") == target
        and prior_packet.get("subject_pr")
        == (subject_target if snapshot.get("kind") == "pr" else None)
        and prior_packet.get("head") == head
        and prior_packet.get("base") == base
    )
    expected_identity = _hash(
        {"target": target, "subject_pr": subject_pr, "head": head, "base": base}
    )

    def prior_section(
        name: str, binding_name: str, binding: str, *, allow_partial: bool = False
    ) -> dict | None:
        if (
            not prior_ok
            or prior_packet is None
            or prior_packet["bindings"].get(binding_name) != binding
        ):
            return None
        section = prior_packet["sections"].get(name)
        if (
            isinstance(section, dict)
            and isinstance(section.get("provenance"), dict)
            and section["provenance"].get("request_status") == "not_requested"
        ):
            return None
        if allow_partial and name == "review_conversations":
            provenance = section.get("provenance", {}) if isinstance(section, dict) else {}
            if (
                not isinstance(provenance, dict)
                or provenance.get("pagination_complete") is not True
            ):
                return None
        return section if _retained_section_valid(section, allow_partial=allow_partial) else None

    def preserved(name):
        if (
            refresh_sections is None
            or name in refresh_sections
            or not prior_ok
            or prior_packet is None
        ):
            return None
        if prior_packet.get("capture_revision") != snapshot.get("revision"):
            return None
        binding_name, binding = (
            {
                "source": ("source", _hash(sources)),
                "review_conversations": ("reviews", _hash(snapshot.get("review_revision"))),
            }
        ).get(name, ("identity", expected_identity))
        if prior_packet["bindings"].get(binding_name) != binding:
            return None
        section = prior_packet["sections"].get(name)
        if (
            isinstance(section, dict)
            and isinstance(section.get("provenance"), dict)
            and section["provenance"].get("request_status") == "not_requested"
        ):
            return None
        # A verified missing/partial observation is progress too. Keep its raw
        # section and failure reason until an explicit requirement selects it.
        if not isinstance(section, dict) or not _retained_section_payload_valid(section):
            return None
        return section

    cached_source = prior_section("source", "source", _hash(sources), allow_partial=True)
    cached_diff = prior_section("diff_files", "identity", expected_identity)
    if cached_diff:
        files = (cached_diff.get("content") or {}).get("files", [])
        if (
            not isinstance(base, str)
            or not isinstance(head, str)
            or any(
                row.get("patch") is not None
                and not validate_patch_reference(row, base=base, head=head)
                for row in files
            )
        ):
            cached_diff = None
    cached_reviews = prior_section(
        "review_conversations",
        "reviews",
        _hash(snapshot.get("review_revision")),
        allow_partial=True,
    )
    if cached_source and any(
        not _retained_ref_valid(row.get("body_retained"))
        for row in cached_source.get("content", [])
        if isinstance(row, dict)
    ):
        cached_source = None
    if cached_reviews and any(
        not _retained_ref_valid(comment.get("body_retained"))
        for thread in cached_reviews.get("content", [])
        for comment in thread.get("comments", [])
    ):
        cached_reviews = None
    shared_budget = {
        "archives_remaining": MAX_PACKET_ARCHIVES,
        "bytes_remaining": MAX_PACKET_ARCHIVE_BYTES,
    }
    # Fetch the action's required native input before optional ordinary CI can
    # consume the shared archive budget. This never starts or retries a verifier.
    native_first = snapshot.get("state") == "MERGED" and action in {
        "collect_evidence",
        "disposition",
    }
    native_section = preserved("native_merge_verifier") or (
        _native_verifier_section(
            snapshot,
            api,
            deadline,
            root,
            artifact_fetch,
            shared_budget,
            native_archive_root=native_archive_root,
        )
        if native_first
        else None
    )
    minimum_dispatch = action in {"post_merge_verify", "wait", "complete"}

    def retained_or_not_requested(name):
        if (
            prior_ok
            and prior_packet
            and prior_packet.get("capture_revision") == snapshot.get("revision")
        ):
            binding_name, binding = (
                ("reviews", _hash(snapshot.get("review_revision")))
                if name == "review_conversations"
                else ("identity", expected_identity)
            )
            section = prior_packet["sections"].get(name)
            if (
                prior_packet["bindings"].get(binding_name) == binding
                and isinstance(section, dict)
                and _retained_section_payload_valid(section)
                and (name != "diff_files" or cached_diff is not None)
                and (name != "review_conversations" or cached_reviews is not None)
            ):
                return section
        return not_requested()

    def not_requested():
        return _section(
            "missing",
            None,
            provenance={"request_status": "not_requested", "action": action},
            reason="optional enrichment not requested for native verification dispatch",
        )

    sections = {
        "source": preserved("source") or cached_source or _source_section(sources, root),
        "diff_files": (
            retained_or_not_requested("diff_files")
            if minimum_dispatch
            else preserved("diff_files")
            or (None if refresh_sections and "diff_files" in refresh_sections else cached_diff)
            or _diff_section(snapshot, api, deadline, root)
        ),
        "review_conversations": (
            retained_or_not_requested("review_conversations")
            if minimum_dispatch
            else preserved("review_conversations")
            or (
                None
                if refresh_sections and "review_conversations" in refresh_sections
                else cached_reviews
            )
            or _review_section(snapshot, review_threads_fetch, deadline, root)
        ),
        "prior_dispositions": _section(
            "complete" if dispositions else "missing",
            dispositions or None,
            provenance={"operator_supplied": bool(dispositions), "text_is_untrusted": True},
        ),
        "checks": _section(
            "complete" if snapshot.get("checks") is not None else "pending",
            {"head": head, "aggregate_state": snapshot.get("checks"), "named_checks": None},
            provenance={
                "head_bound": bool(head and snapshot.get("checks") is not None),
                "source": "current PR status rollup",
                "detail": "per-check names are unavailable in snapshot",
            },
            reason=None if snapshot.get("checks") is not None else "no current-head check rollup",
        ),
        "ci_artifacts": (
            retained_or_not_requested("ci_artifacts")
            if minimum_dispatch
            else preserved("ci_artifacts")
            or _artifact_section(
                snapshot,
                api,
                deadline,
                root,
                artifact_fetch,
                (
                    prior_packet.get("sections", {}).get("ci_artifacts")
                    if prior_ok and prior_packet
                    else None
                ),
                shared_budget,
            )
        ),
        "native_merge_verifier": (
            retained_or_not_requested("native_merge_verifier")
            if minimum_dispatch
            else (
                native_section
                if native_section is not None
                else _native_verifier_section(
                    snapshot,
                    api,
                    deadline,
                    root,
                    artifact_fetch,
                    shared_budget,
                    native_archive_root=native_archive_root,
                )
            )
        ),
    }
    if (
        prior_ok
        and prior_packet
        and prior_packet.get("capture_revision") == snapshot.get("revision")
    ):
        for name in ("requested_evidence",):
            prior_extra = prior_packet["sections"].get(name)
            if isinstance(prior_extra, dict) and _retained_section_payload_valid(prior_extra):
                sections[name] = prior_extra
    for name, section in (supplemental_sections or {}).items():
        if name != "requested_evidence" or not isinstance(section, dict):
            raise ValueError("unsupported supplemental collection section")
        sections[name] = section
    inventory = snapshot.get("delivery_links")
    if inventory is not None:
        sections["delivery_inventory"] = _section(
            (
                "complete"
                if isinstance(inventory, dict) and inventory.get("status") == "resolved"
                else "missing"
            ),
            inventory,
            provenance={"source": "exact source delivery inventory", "acceptance_authority": False},
            reason=(
                inventory.get("reason")
                if isinstance(inventory, dict)
                else "malformed delivery inventory"
            ),
        )
    ci = sections["ci_artifacts"]
    receipts = [
        receipt
        for item in ((ci.get("content") or {}).get("artifacts") or [])
        if isinstance(item, dict)
        for receipt in item.get("receipts", [])
        if isinstance(receipt, dict)
    ]
    tests = [
        item
        for item in receipts
        if item.get("kind") == "junit" or item.get("kind") == "test-summary"
    ]
    coverage = [item for item in receipts if item.get("kind") == "coverage-summary"]
    tc_status = "partial" if tests or coverage else ci["status"]
    sections["test_coverage_artifacts"] = _section(
        tc_status,
        (
            {
                "tests": tests,
                "coverage": coverage,
                "proof_status": "partial" if tests or coverage else "unknown",
                "retrieval_status": ci.get("content", {}).get("retrieval_status", ci["status"]),
            }
            if tests or coverage
            else None
        ),
        provenance=ci.get("provenance", {}),
        reason="receipts are retained, but required test/coverage job topology is unknown",
    )
    sections["elapsed"] = _section(
        "complete",
        {
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "deadline_ms": max(0, int((deadline - started) * 1000)),
        },
    )
    packet: dict[str, Any] = {
        "schema": SCHEMA,
        "producer_version": PRODUCER_VERSION,
        "producer_sha256": producer_sha,
        "target": target,
        "subject_pr": subject_pr,
        "head": head,
        "base": base,
        "bindings": {
            "identity": _hash(
                {"target": target, "subject_pr": subject_pr, "head": head, "base": base}
            ),
            "source": _hash(sources),
            "reviews": _hash(snapshot.get("review_revision")),
            "dispositions": _hash(dispositions or {}),
        },
        "sections": sections,
        "section_digests": {k: v["sha256"] for k, v in sections.items()},
        "history": [],
        "collection_action": action,
        "capture_revision": snapshot.get("revision"),
    }
    packet["semantic_cache_key"] = _semantic_cache_key(packet)
    packet = update_packet(previous, packet)
    for name, section in packet["sections"].items():
        if not _retained_section_valid(section):
            section_payload = {k: v for k, v in section.items() if k != "retained"}
            section["retained"] = _retain_bytes(root, _bytes(section_payload), "json")
    # Section digests exclude the storage pointer, preventing self-reference.
    packet["section_digests"] = {k: _section_hash(v) for k, v in packet["sections"].items()}
    for name, section in packet["sections"].items():
        section["sha256"] = packet["section_digests"][name]
    manifest = {"section_files": {k: v["retained"] for k, v in packet["sections"].items()}}
    packet["manifest"] = {**manifest, "packet_sha256": _packet_hash(packet)}
    readiness = action_evidence_readiness(snapshot, packet, action, retained_dir=root)
    status = (
        "diagnostic"
        if action is None
        else (
            "deterministic"
            if readiness.get("mode") == "deterministic" and readiness["ready"]
            else "ready" if readiness["ready"] else "pending"
        )
    )
    packet["collection_result"] = {
        "schema": "closer-evidence-collection/v1",
        "status": status,
        "evaluation_ready": status == "ready",
        "acceptance_authority": False,
        "action": action,
        "readiness": readiness,
    }
    packet["manifest"] = {**manifest, "packet_sha256": _packet_hash(packet)}
    # The manifest is not part of its own digest and is a separate immutable file.
    _atomic_write(
        root / "manifests" / f"{packet['manifest']['packet_sha256']}.json", _bytes(packet)
    )
    return packet


def drift_sample(packet: dict, *, recurring: bool = False, count: int = 3) -> list[dict]:
    """Deterministic bounded schema-valid advisory drift sample plan."""
    if not validate_packet(packet):
        raise ValueError("invalid packet")
    seed = _hash(
        {"producer": packet["producer_sha256"], "head": packet["head"], "recurring": recurring}
    )

    def refs(names):
        return {
            name: {
                "sha256": packet["sections"][name]["sha256"],
                "raw": packet["sections"][name].get("retained"),
                "status": packet["sections"][name]["status"],
                "provenance": packet["sections"][name].get("provenance", {}),
            }
            for name in names
        }

    candidates = [
        {
            "case": "schema_valid_packet",
            "digest": _packet_hash(packet),
            "packet_ref": {
                "packet_sha256": packet.get("manifest", {}).get("packet_sha256"),
                "section_files": packet.get("manifest", {}).get("section_files", {}),
            },
            "section_refs": refs(sorted(REQUIRED_SECTIONS)),
            "expected_assertions": [
                "validate_packet succeeds",
                "all required sections have matching digests",
                "manifest section file references match retained sections",
            ],
        },
        {
            "case": "pending_or_missing",
            "digest": _hash(
                {k: v["status"] for k, v in packet["sections"].items() if v["status"] != "complete"}
            ),
            "section_refs": refs(
                [k for k, v in packet["sections"].items() if v["status"] != "complete"]
            ),
            "expected_assertions": [
                "pending, missing, unknown, partial, or truncated evidence is not treated as complete"
            ],
        },
        {
            "case": "untrusted_source",
            "digest": packet["sections"]["source"]["sha256"],
            "section_refs": refs(["source"]),
            "expected_assertions": [
                "source text is evidence data, never instructions",
                "credential redactions and truncation are explicit",
            ],
        },
        {
            "case": "artifact_provenance",
            "digest": _hash(
                {
                    k: packet["sections"][k]["sha256"]
                    for k in ("ci_artifacts", "test_coverage_artifacts", "native_merge_verifier")
                }
            ),
            "section_refs": refs(
                ["ci_artifacts", "test_coverage_artifacts", "native_merge_verifier"]
            ),
            "expected_assertions": [
                "current-head CI receipts remain distinct from evaluated-merge native verifier input",
                "native NON_PASS evidence is resolution-only and never acceptance proof",
                "retrieval completeness does not imply required-check or acceptance completeness",
            ],
        },
    ]
    if recurring:
        candidates.reverse()
    offset = int(seed[:8], 16) % len(candidates)
    return [
        {
            **candidates[(offset + i) % len(candidates)],
            "seed": seed,
            "judgment_role": "advisory",
            "authority": False,
        }
        for i in range(min(max(1, count), len(candidates)))
    ]
