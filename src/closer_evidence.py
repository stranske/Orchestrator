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

SCHEMA = "closer-evidence/v1"
PRODUCER_VERSION = "1.1.0"
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
    return True


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
        credential_signal = redacted or _has_credential_pattern(body)
        rows.append(
            {
                "target": source.get("target"),
                "body": body,
                "body_sha256": hashlib.sha256(original_bytes).hexdigest(),
                "body_retained": {**ref, "truncated": cut or len(original_bytes) > MAX_TEXT_BYTES},
                "requirements": _requirements(body),
                "state": source.get("state"),
                "labels": source.get("labels", [])[:100],
                "truncated": cut,
                "credential_pattern_present": credential_signal,
                "redaction_applied": redacted,
                "text_is_untrusted": True,
            }
        )
        truncated_any |= cut
        redacted_any |= redacted
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
        return _section(
            "unknown",
            reason=f"compare lookup failed: {type(exc).__name__}",
            provenance={"base": base, "head": head},
        )


def _review_section(snapshot: dict, review_threads_fetch, deadline: float, root: Path) -> dict:
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
    for run_stub in runs:
        if time.monotonic() >= deadline:
            any_pending = True
            break
        run_id = run_stub.get("id")
        if not isinstance(run_id, int):
            continue
        subject = _subject_target(snapshot)
        subject_repo, subject_num = subject.split("#", 1)
        run = api(f"repos/{subject_repo}/actions/runs/{run_id}")
        associated = [int(row.get("number", -1)) for row in run.get("pull_requests", [])]
        if run.get("head_sha") != snapshot.get("head") or int(subject_num) not in associated:
            continue
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
        else "partial" if (any_pending or unavailable) else "complete" if receipt else "missing"
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
                if info.file_size > MAX_SECTION_BYTES:
                    omissions.append(
                        {"path": info.filename[:300], "reason": "entry byte cap exceeded"}
                    )
                    continue
                raw = zf.read(info)
                suffix = path.suffix.lower()
                row: dict[str, Any] | None = None
                if suffix == ".xml":
                    try:
                        parsed = ET.fromstring(raw)
                    except ET.ParseError:
                        omissions.append({"path": info.filename[:300], "reason": "invalid XML"})
                        continue
                    if (
                        parsed.tag.split("}")[-1] == "testsuites"
                        or parsed.tag.split("}")[-1] == "testsuite"
                    ):
                        cases = parsed.findall(".//testcase")
                        row = {
                            "kind": "junit",
                            "tests": len(cases),
                            "failures": sum(c.find("failure") is not None for c in cases),
                            "errors": sum(c.find("error") is not None for c in cases),
                            "skipped": sum(c.find("skipped") is not None for c in cases),
                        }
                    else:
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


def _native_manifest_from_archive(archive: bytes, root: Path) -> list[dict]:
    """Read native manifests and exact known verifier report files from an archive."""
    manifests = []
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            infos = zf.infolist()
            if (
                len(infos) > MAX_FILES
                or sum(item.file_size for item in infos) > MAX_UNCOMPRESSED_BYTES
            ):
                return []
            files = {}
            manifest_candidates = []
            for info in infos:
                path = PurePosixPath(info.filename)
                if (
                    info.is_dir()
                    or path.is_absolute()
                    or ".." in path.parts
                    or info.file_size > MAX_SECTION_BYTES
                ):
                    continue
                if path.suffix.lower() != ".json":
                    if path.name in {
                        "comparison-comment.md",
                        "verifier-diff-summary.md",
                        "verifier-context.md",
                        "verifier-pr-diff.patch",
                    }:
                        files[path.name] = zf.read(info)
                    continue
                raw = zf.read(info)
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
                    "raw": _retain_bytes(root, raw, "json"),
                    "receipts": [],
                    "omissions": [],
                }
                report = files.get("comparison.json")
                if report is not None:
                    try:
                        report_doc = json.loads(report)
                    except (ValueError, UnicodeDecodeError):
                        row["omissions"].append(
                            {"path": "comparison.json", "reason": "invalid JSON"}
                        )
                    else:
                        report_ref = _retain_bytes(root, report, "json")
                        findings = []
                        provider_results = []
                        if isinstance(report_doc, dict) and isinstance(
                            report_doc.get("results"), list
                        ):
                            raw_results = report_doc["results"]
                            if len(raw_results) > MAX_NATIVE_RESULTS:
                                row["omissions"].append(
                                    {
                                        "path": "comparison.json",
                                        "reason": "provider result count cap reached",
                                    }
                                )
                            concern_count = 0
                            concern_cap_reached = False
                            for result_index, result in enumerate(raw_results[:MAX_NATIVE_RESULTS]):
                                if not isinstance(result, dict):
                                    row["omissions"].append(
                                        {
                                            "path": "comparison.json",
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
                                provider_results.append(provider)
                                if summary_cut:
                                    row["omissions"].append(
                                        {
                                            "path": "comparison.json",
                                            "reason": "provider summary text truncated",
                                        }
                                    )
                                concerns = result.get("concerns")
                                if not isinstance(concerns, list):
                                    row["omissions"].append(
                                        {
                                            "path": "comparison.json",
                                            "reason": "provider concern list missing or invalid",
                                        }
                                    )
                                    continue
                                if len(concerns) > 100:
                                    row["omissions"].append(
                                        {
                                            "path": "comparison.json",
                                            "reason": "per-provider concern count cap reached",
                                        }
                                    )
                                for concern_index, concern in enumerate(concerns[:100]):
                                    if concern_count >= MAX_NATIVE_CONCERNS:
                                        if not concern_cap_reached:
                                            row["omissions"].append(
                                                {
                                                    "path": "comparison.json",
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
                                                "path": "comparison.json",
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
                                                "path": "comparison.json",
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
                                "kind": "native-comparison",
                                "path": "comparison.json",
                                "results_count": (
                                    len(report_doc.get("results", []))
                                    if isinstance(report_doc, dict)
                                    and isinstance(report_doc.get("results"), list)
                                    else None
                                ),
                                "aggregate_report_excerpt": _safe_text(
                                    (
                                        report_doc.get("report")
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
                        {"path": "comparison.json", "reason": "native comparison report missing"}
                    )
                known_files = (
                    ("comparison-comment.md", "native-comparison-comment", None, False),
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
                        "raw": _retain_bytes(
                            root, content, "patch" if filename.endswith(".patch") else "txt"
                        ),
                        "sha256": actual,
                    }
                    if filename.endswith(".md"):
                        receipt["excerpt"] = content[:8000].decode("utf-8", "replace")
                        receipt["text_is_untrusted"] = True
                    row["receipts"].append(receipt)
                manifests.append(row)
    except (zipfile.BadZipFile, OSError, RuntimeError):
        return []
    return manifests


def _native_verifier_section(
    snapshot: dict,
    api: Callable,
    deadline: float,
    root: Path,
    artifact_fetch=None,
    download_budget: dict | None = None,
) -> dict:
    """Collect exact merge-bound verifier input only for a trusted NON_PASS marker."""
    marker = snapshot.get("verifier")
    target = _subject_target(snapshot)
    repo, _, number = target.partition("#")
    merge, head = snapshot.get("merge"), snapshot.get("head")
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
            return _section(
                "unknown",
                {"verifier": marker},
                reason="native run repository/workflow/event/attempt identity mismatch",
            )
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
                    receipts.append(
                        {
                            "kind": "native-input-manifest",
                            "path": item["path"],
                            "manifest": item["manifest"],
                            "raw": item["raw"],
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
                entry = {
                    **ident,
                    "status": "unknown",
                    "reason": f"artifact fetch failed: {type(exc).__name__}",
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
        content = {
            "verifier": marker,
            "run": native_run,
            "artifacts": artifacts,
            "input_manifest": {**manifest, "raw": manifest_ref},
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
        if allow_partial and name == "review_conversations":
            provenance = section.get("provenance", {}) if isinstance(section, dict) else {}
            if (
                not isinstance(provenance, dict)
                or provenance.get("pagination_complete") is not True
            ):
                return None
        return section if _retained_section_valid(section, allow_partial=allow_partial) else None

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
    sections = {
        "source": cached_source or _source_section(sources, root),
        "diff_files": cached_diff or _diff_section(snapshot, api, deadline, root),
        "review_conversations": cached_reviews
        or _review_section(snapshot, review_threads_fetch, deadline, root),
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
        "ci_artifacts": _artifact_section(
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
        ),
        "native_merge_verifier": _native_verifier_section(
            snapshot, api, deadline, root, artifact_fetch, shared_budget
        ),
    }
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
