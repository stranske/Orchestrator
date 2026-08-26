#!/usr/bin/env python3
"""Append-only authority for explicit provider rate-limit incidents."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HANDOFF = Path(os.environ.get("HANDOFF_DIR", Path.home() / ".codex" / "handoff"))
INCIDENT_FILE = HANDOFF / "rate-limit-incidents.ndjson"
LOCK_FILE = HANDOFF / "rate-limit-incidents.ndjson.lock"
SHED_DIR = HANDOFF / "capacity-shed"
SCHEMA = "rate-limit-incident/v1"
MAX_EVIDENCE_EXCERPT = 500
DEFAULT_COOLDOWN_S = 6 * 60 * 60
MAX_DEDUP_SCAN_BYTES = 1024 * 1024


def _redact_bounded(text: str, max_len: int = MAX_EVIDENCE_EXCERPT) -> str:
    text = str(text or "")
    patterns = (
        r"sk-[A-Za-z0-9]{12,}",
        r"github_pat_[A-Za-z0-9_]{12,}",
        r"xox[baprs]-[A-Za-z0-9-]{10,}",
        r"Bearer\s+[^\s]+",
        r"(?:api[_-]?key|token|secret|password)\s*[=:]\s*[^\s,;]+",
        r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
    )
    for pattern in patterns:
        text = re.sub(pattern, "[REDACTED]", text, flags=re.IGNORECASE)
    return text[:max_len] + ("...[TRUNCATED]" if len(text) > max_len else "")


def _hash_evidence(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:16]


def _contains_explicit_resource_exhausted(text: str) -> bool:
    """Recognize provider-shaped exhaustion, not implementation/review prose."""
    return bool(
        re.search(
            r"(?im)^\s*(?:\[resource_exhausted\]|resource_exhausted)\s*$"
            r"|^\s*(?:error|status|code|response|provider\s+error)\b[^\n]{0,120}"
            r"\bresource_exhausted\b"
            r'|"(?:status|code)"\s*:\s*"RESOURCE_EXHAUSTED"',
            text,
        )
    )


def classify_provider_failure(text: str) -> tuple[str, str, str]:
    """Return an authoritative category only for explicit capacity evidence."""
    if not isinstance(text, str) or not text:
        return "unknown", "non_authoritative", "none"
    if _contains_explicit_resource_exhausted(text):
        return "capacity", "resource_exhausted", "high"
    if re.search(
        r"(?:out of usage|usage exhausted|quota exhausted|capacity exhausted)", text, re.I
    ):
        return "quota", "quota_exhausted", "high"
    # A bare number is ordinary task prose (issue 429, 429 lines, 429 tests).
    # Require either the provider's standard phrase or an error/status/code
    # anchor immediately before the numeric status.
    if re.search(r"\btoo many requests\b", text, re.I) or re.search(
        r"\b(?:http(?:\s+status)?|status(?:\s+code)?|error(?:\s+code)?|response\s+code)"
        r"\s*[:=]?\s*429\b",
        text,
        re.I,
    ):
        return "rate_limit", "http_429", "high"
    # A generic mention of rate limits is common in successful task summaries and
    # implementation prompts. Require failure-shaped language before shedding.
    if re.search(
        r"(?:rate[ -]?limit(?:ed)?(?:\s+(?:was\s+))?(?:exceeded|exhausted|reached|hit))"
        r"|(?:(?:exceeded|exhausted|reached|hit)\s+(?:an?\s+)?(?:primary\s+|secondary\s+)?rate[ -]?limit)",
        text,
        re.I,
    ):
        return "rate_limit", "rate_limit_exhausted", "high"
    # ActionRequiredError is context, never capacity evidence by itself.
    return "unknown", "non_authoritative", "none"


def is_authoritative_error(text: str) -> bool:
    return classify_provider_failure(text)[2] == "high"


def stdout_carries_capacity_evidence(text: str) -> bool:
    """Whether successful stdout is explicit enough to include in failure classification."""
    if not isinstance(text, str) or not text:
        return False
    return _contains_explicit_resource_exhausted(text) or bool(
        re.search(r"(?im)^\s*ActionRequiredError\b[^\n]*(?:out of usage|quota exhausted)", text)
    )


def get_structured_evidence(
    error_text: str, agent: str, surface: str, run_id: str | None = None, target: str | None = None
) -> dict[str, Any]:
    category, subcategory, confidence = classify_provider_failure(error_text)
    return {
        "agent": agent,
        "surface": surface,
        "run_id": run_id,
        "target": target,
        "category": category,
        "subcategory": subcategory,
        "confidence": confidence,
        "is_authoritative": confidence == "high",
        "should_shed": confidence == "high",
        "evidence_excerpt": _redact_bounded(error_text),
        "evidence_hash": _hash_evidence(error_text),
    }


def _ensure_paths() -> None:
    HANDOFF.mkdir(parents=True, exist_ok=True)
    SHED_DIR.mkdir(parents=True, exist_ok=True)


def _acquire_lock() -> int:
    import fcntl

    _ensure_paths()
    fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _release_lock(fd: int) -> None:
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _idempotency_key(run_id: str, agent: str, category: str, surface: str) -> str:
    del surface
    # The same failed run can be observed by both the synchronous dispatcher and
    # the completion reconciler. It is one incident, regardless of observer.
    return f"{run_id}|{agent}|{category}"


def _existing_idempotency_key(key: str) -> str | None:
    if not INCIDENT_FILE.exists():
        return None
    try:
        with INCIDENT_FILE.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            start = max(0, size - MAX_DEDUP_SCAN_BYTES)
            handle.seek(start)
            if start:
                handle.readline()  # Drop the first partial row from the bounded suffix.
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("idempotency_key") == key:
                return row.get("incident_id")
    except OSError:
        return None
    return None


def _cooldown_seconds(category: str) -> int | None:
    if category not in {"rate_limit", "quota", "capacity"}:
        return None
    try:
        return max(0, int(os.environ.get("ORCH_PROVIDER_COOLDOWN_S", str(DEFAULT_COOLDOWN_S))))
    except ValueError:
        return DEFAULT_COOLDOWN_S


def ensure_shed(
    agent: str, *, category: str, incident_id: str, expires_at: int | None = None
) -> bool:
    """Write one JSON marker; it intentionally never records another incident."""
    _ensure_paths()
    marker = SHED_DIR / agent
    if marker.exists():
        try:
            existing = json.loads(marker.read_text(encoding="utf-8"))
            existing_expiry = existing.get("expires_at") if isinstance(existing, dict) else None
            if (
                isinstance(existing_expiry, bool)
                or not isinstance(existing_expiry, (int, float))
                or not math.isfinite(existing_expiry)
                or existing_expiry > time.time()
            ):
                return True
            marker.unlink()
        except (OSError, json.JSONDecodeError):
            # Legacy empty or malformed markers remain manual, authoritative sheds.
            return True
    payload = {"created_at": int(time.time()), "category": category, "incident_id": incident_id}
    if expires_at is not None:
        payload["expires_at"] = expires_at
    try:
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return True
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            marker.unlink()
        except OSError:
            pass
        return False
    return True


def record_incident(
    *,
    agent: str,
    surface: str,
    category: str,
    status: str = "observed",
    provider: str | None = None,
    target: str | None = None,
    run_id: str | None = None,
    evidence: str = "",
    timestamp: int | None = None,
    shed: bool = True,
    credential_pool: str | None = None,
    resource: str | None = None,
    reset_at: int | None = None,
    reroute: str | None = None,
    next_success_at: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append exactly one incident line while holding an exclusive fcntl lock."""
    if not agent or not surface or not category:
        raise ValueError("agent, surface, and category are required")
    timestamp = int(timestamp or time.time())
    stable_run_id = run_id or f"sync:{_hash_evidence(str(target) + evidence)}"
    key = _idempotency_key(stable_run_id, agent, category, surface)
    incident_id = _hash_evidence(key)
    record: dict[str, Any] = {
        "schema": SCHEMA,
        "incident_id": incident_id,
        "idempotency_key": key,
        "ts": timestamp,
        "run_id": stable_run_id,
        "agent": agent,
        "provider": provider or agent,
        "surface": surface,
        "category": category,
        "status": status,
        "target": target,
        "evidence_hash": _hash_evidence(evidence),
        "evidence_excerpt": _redact_bounded(evidence),
    }
    record.update(
        {
            key: value
            for key, value in {
                "credential_pool": credential_pool,
                "resource": resource,
                "reset_at": reset_at,
                "reroute": reroute,
                "next_success_at": next_success_at,
            }.items()
            if value is not None
        }
    )
    if extra:
        record["extra"] = {key: value for key, value in extra.items() if value is not None}
    lock_fd = _acquire_lock()
    try:
        existing = _existing_idempotency_key(key)
        if existing:
            return {"status": "ok", "deduped": True, "incident_id": existing}
        line = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        ledger_fd = os.open(INCIDENT_FILE, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(ledger_fd, "wb") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        _release_lock(lock_fd)
    cooldown = _cooldown_seconds(category)
    shed_created = (
        ensure_shed(
            agent,
            category=category,
            incident_id=incident_id,
            expires_at=(timestamp + cooldown if cooldown is not None else None),
        )
        if shed
        else False
    )
    return {"status": "ok", "deduped": False, "incident_id": incident_id, "shed": shed_created}


def check_shed(agent: str) -> bool:
    return (SHED_DIR / agent).exists()


def clear_shed(agent: str) -> bool:
    try:
        (SHED_DIR / agent).unlink()
        return True
    except FileNotFoundError:
        return False


def _selftest() -> int:
    global HANDOFF, INCIDENT_FILE, LOCK_FILE, SHED_DIR
    old = HANDOFF, INCIDENT_FILE, LOCK_FILE, SHED_DIR
    tmp = Path(tempfile.mkdtemp(prefix="rate-incidents-selftest-"))
    try:
        HANDOFF, INCIDENT_FILE = tmp, tmp / "incidents.ndjson"
        LOCK_FILE, SHED_DIR = tmp / "incidents.lock", tmp / "shed"
        assert not is_authoritative_error("ActionRequiredError")
        assert not is_authoritative_error("implemented rate limit handling")
        assert is_authoritative_error("ActionRequiredError: out of usage")
        assert is_authoritative_error("resource_exhausted") and is_authoritative_error("HTTP 429")
        INCIDENT_FILE.write_text('{"prior":true}\n')
        first = record_incident(
            agent="codex",
            surface="selftest",
            category="quota",
            run_id="r1",
            evidence="quota exhausted",
        )
        assert len(INCIDENT_FILE.read_text().splitlines()) == 2
        assert record_incident(
            agent="codex",
            surface="selftest",
            category="quota",
            run_id="r1",
            evidence="quota exhausted",
        )["deduped"]
        assert first["incident_id"]
        print("rate_incidents.py selftest: OK")
        return 0
    finally:
        HANDOFF, INCIDENT_FILE, LOCK_FILE, SHED_DIR = old
        shutil.rmtree(tmp, ignore_errors=True)


def _load_rows() -> list[dict[str, Any]]:
    if not INCIDENT_FILE.exists():
        return []
    rows = []
    for line in INCIDENT_FILE.read_text(errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true")
    subparsers = parser.add_subparsers(dest="command")
    classify_parser = subparsers.add_parser(
        "classify", help="classify evidence from --evidence or stdin"
    )
    classify_parser.add_argument("--evidence")
    record_parser = subparsers.add_parser("record", help="append one authoritative incident")
    record_parser.add_argument("--agent", required=True)
    record_parser.add_argument("--provider")
    record_parser.add_argument("--surface", required=True)
    record_parser.add_argument("--target")
    record_parser.add_argument("--run-id")
    record_parser.add_argument("--credential-pool")
    record_parser.add_argument("--resource")
    record_parser.add_argument("--reset-at", type=int)
    record_parser.add_argument("--reroute")
    record_parser.add_argument("--next-success-at", type=int)
    record_parser.add_argument(
        "--category", choices=("auto", "rate_limit", "quota", "capacity"), default="auto"
    )
    record_parser.add_argument("--status", default="observed")
    record_parser.add_argument("--evidence")
    record_parser.add_argument("--no-shed", action="store_true")
    subparsers.add_parser("summary", help="summarize the append-only incident ledger")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    if args.command == "classify":
        evidence = args.evidence if args.evidence is not None else sys.stdin.read()
        print(json.dumps(get_structured_evidence(evidence, "unknown", "cli"), sort_keys=True))
        return 0
    if args.command == "record":
        evidence = args.evidence if args.evidence is not None else sys.stdin.read()
        category, subcategory, confidence = classify_provider_failure(evidence)
        if args.category != "auto":
            category = args.category
        elif confidence != "high":
            print(json.dumps({"status": "ignored", "reason": "non_authoritative"}, sort_keys=True))
            return 2
        result = record_incident(
            agent=args.agent,
            provider=args.provider,
            surface=args.surface,
            target=args.target,
            run_id=args.run_id,
            category=category,
            status=args.status,
            evidence=evidence,
            shed=not args.no_shed,
            credential_pool=args.credential_pool,
            resource=args.resource,
            reset_at=args.reset_at,
            reroute=args.reroute,
            next_success_at=args.next_success_at,
            extra={"subcategory": subcategory},
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.command == "summary":
        rows = _load_rows()
        by_agent: dict[str, int] = {}
        by_category: dict[str, int] = {}
        for row in rows:
            agent = str(row.get("agent") or "unknown")
            category = str(row.get("category") or "unknown")
            by_agent[agent] = by_agent.get(agent, 0) + 1
            by_category[category] = by_category.get(category, 0) + 1
        print(
            json.dumps(
                {"total": len(rows), "by_agent": by_agent, "by_category": by_category},
                sort_keys=True,
            )
        )
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
