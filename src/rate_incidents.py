#!/usr/bin/env python3
"""Append-only authority for explicit provider rate-limit incidents."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import credential_redaction

HANDOFF = Path(os.environ.get("HANDOFF_DIR", Path.home() / ".codex" / "handoff"))
INCIDENT_FILE = HANDOFF / "rate-limit-incidents.ndjson"
LOCK_FILE = HANDOFF / "rate-limit-incidents.ndjson.lock"
SHED_DIR = HANDOFF / "capacity-shed"
SCHEMA = "rate-limit-incident/v1"
MAX_EVIDENCE_EXCERPT = 500
DEFAULT_COOLDOWN_S = 6 * 60 * 60
MAX_DEDUP_SCAN_BYTES = 1024 * 1024

# codex `exec --json` events that mean the run DID something. An item whose own type is `error` is the
# harness's warning (every run prints "Under-development features enabled"), not work.
CODEX_WORK_EVENTS = frozenset({"item.started", "item.updated", "item.completed", "turn.completed"})


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
    # The shared shape list, after the six above: it holds what they lack, a GitHub token above all.
    # A run whose completion step died is classified from its unscrubbed log, and its excerpt lands
    # here.
    text = credential_redaction.redact(text)
    return text[:max_len] + ("...[TRUNCATED]" if len(text) > max_len else "")


def _hash_evidence(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:16]


def _has_provider_error_context(line: str) -> bool:
    """Require an error envelope, including Gemini's glog error prefix."""
    return bool(
        re.match(
            r"\s*(?:E\d{4}\s+\d{2}:\d{2}:\d{2}\.\d+\s+\d+\s+[^\]\n]+\]\s*)?"
            r"(?:agent\s+executor\s+error|provider\s+error|error|ActionRequiredError)\s*:",
            line,
            re.I,
        )
    )


def _contains_explicit_resource_exhausted(text: str) -> bool:
    """Recognize provider-shaped exhaustion, not implementation/review prose."""
    return bool(
        re.search(
            r"(?im)^\s*(?:\[resource_exhausted\]|resource_exhausted)\s*$"
            r"|^\s*(?:error|status|code|response|provider\s+error)\b[^\n]{0,120}"
            r"\bresource_exhausted\b"
            r'|^\s*\{[^\n]{0,120}"(?:status|code)"\s*:\s*"RESOURCE_EXHAUSTED"',
            text,
        )
    ) or any(
        _has_provider_error_context(line) and re.search(r"\bRESOURCE_EXHAUSTED\b", line, re.I)
        for line in text.splitlines()
    )


def classify_provider_failure(text: str) -> tuple[str, str, str]:
    """Return an authoritative category only for explicit capacity evidence."""
    if not isinstance(text, str) or not text:
        return "unknown", "non_authoritative", "none"
    if re.search(
        r"(?:out of usage|usage exhausted|quota exhausted|capacity exhausted)"
        r"|(?:you(?:'|’)ve hit your usage limit)"
        r"|(?:credit(?:s| balance)? (?:exhausted|depleted|used up|too low))",
        text,
        re.I,
    ) or any(
        _has_provider_error_context(line)
        and re.search(r"\b(?:individual\s+)?quota\s+reached\b", line, re.I)
        for line in text.splitlines()
    ):
        return "quota", "quota_exhausted", "high"
    if _contains_explicit_resource_exhausted(text):
        return "capacity", "resource_exhausted", "high"
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


def parse_relay_reset_at(text: str, *, now: int | None = None) -> int | None:
    """Parse a provider's 'try again at' clock; naive times use host local time."""
    match = re.search(
        r"try again at\s+([A-Za-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+"
        r"(\d{4})\s+(\d{1,2}:\d{2})\s*(AM|PM)\b\s*(UTC|GMT|Z|[+-]\d{2}:?\d{2})?",
        text,
        re.I,
    )
    if not match:
        return None
    month, day, year, clock, meridiem, zone = match.groups()
    try:
        month_format = "%b" if len(month) == 3 else "%B"
        parsed = datetime.strptime(
            f"{month} {day} {year} {clock} {meridiem}",
            f"{month_format} %d %Y %I:%M %p",
        )
        if zone and zone.upper() in {"UTC", "GMT", "Z"}:
            parsed = parsed.replace(tzinfo=timezone.utc)
        elif zone:
            normalized = zone if ":" in zone else zone[:3] + ":" + zone[3:]
            parsed = datetime.fromisoformat(parsed.isoformat() + normalized)
        reset_at = int(parsed.timestamp())
    except (ValueError, OverflowError, OSError):  # a date the platform clock cannot hold
        return None
    return reset_at if reset_at > int(now or time.time()) else None


def generate_relay_decision_id(
    *, when: datetime | None = None, pid: int | None = None, random_hex: str | None = None
) -> str:
    """Coordinator token shape for one pacing decision: ``<utc>-<pid>-<random>``.

    Compact UTC (no ``:``) keeps the token a single log field. The importer and
    ``record-lane-round --relay-decision-id`` accept any nonempty no-whitespace
    token up to ``feedback.RELAY_DECISION_ID_MAX_CHARS`` (176 after the
    ``relay-pacing:id:`` run-id prefix); this helper is the documented production
    form. Prefer the ``generate-relay-decision-id`` CLI so the relay does not need
    ``$ORCH`` on ``PYTHONPATH``.
    """
    stamp = (when or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    token = random_hex if random_hex is not None else secrets.token_hex(4)
    if not token or any(char.isspace() for char in token) or len(token) > 32:
        raise ValueError("random_hex must be a nonempty token of at most 32 characters")
    return f"{stamp}-{pid if pid is not None else os.getpid()}-{token}"


_RELAY_PACING_AGENTS = frozenset({"codex", "claude", "claude_code", "cursor", "muse"})


def _relay_weight_token(value: Any) -> str:
    """Compact numeric weight for the pacing log, or unknown when unmeasured."""
    if value is None or isinstance(value, bool):
        return "unknown"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        return "unknown"
    if number == int(number):
        return str(int(number))
    return f"{number:g}"


def _relay_candidate_weight(row: Mapping[str, Any]) -> str:
    """Prefer the receipt's top-level routing_weight (unmeasured-preferred puts 1.0 there)."""
    if "routing_weight" in row and row.get("routing_weight") is not None:
        return _relay_weight_token(row.get("routing_weight"))
    pace = row.get("pacing")
    if isinstance(pace, Mapping) and pace.get("routing_weight") is not None:
        return _relay_weight_token(pace.get("routing_weight"))
    return "unknown"


def _relay_candidate_state(row: Mapping[str, Any]) -> str:
    state = row.get("state")
    if isinstance(state, str) and state.strip():
        return state.strip()
    pace = row.get("pacing")
    if isinstance(pace, Mapping):
        nested = pace.get("state")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return "unknown"


def relay_log_fields_from_decision(
    decision: Mapping[str, Any], *, candidates: str
) -> dict[str, str]:
    """Importer-valid ``weights`` / ``states`` tokens from pick-agent decision JSON.

    Host ``handoff-relay.sh`` must not re-derive weights from nested ``pacing`` alone:
    Cursor-top ``unmeasured-preferred`` receipts put a neutral ``routing_weight`` on the
    candidate itself while ``pacing`` stays absent — a nested-only jq path logs
    ``*:unknown`` and the weekly #812 pass never sees measured consumption (owner
    2026-10-23). Candidate order follows the relay's candidates list.
    """
    names = [part for part in candidates.split(",") if part]
    if not names or not set(names) <= _RELAY_PACING_AGENTS:
        raise ValueError("candidates must be a nonempty list of supported agents")
    by_agent: dict[str, Mapping[str, Any]] = {}
    raw = decision.get("candidates")
    if isinstance(raw, list):
        for row in raw:
            if not isinstance(row, Mapping):
                continue
            agent = row.get("agent")
            if isinstance(agent, str) and agent in _RELAY_PACING_AGENTS:
                by_agent[agent] = row
    weights = []
    states = []
    for name in names:
        row = by_agent.get(name, {"agent": name})
        weights.append(f"{name}:{_relay_candidate_weight(row)}")
        states.append(f"{name}:{_relay_candidate_state(row)}")
    policy = decision.get("policy")
    return {
        "weights": ",".join(weights),
        "states": ",".join(states),
        "policy": policy if isinstance(policy, str) and policy.strip() else "unmeasured",
    }


def append_relay_pacing_decision(
    *,
    source: str,
    lane: str,
    candidates: str,
    chosen: str,
    weights: str,
    states: str,
    fallback: str,
    decision_id: str | None = None,
    log: Path | None = None,
    when: datetime | None = None,
) -> str:
    """Append one importer-valid pacing line with a decision_id; return that id.

    Call at relay decision time so ``relay-pacing.log`` and the later
    ``record-lane-round --relay-decision-id`` share one token without hand-formatting
    the log line. Omitting ``decision_id`` generates the documented
    ``<utc>-<pid>-<random>`` shape.
    """
    import feedback

    if source not in _RELAY_PACING_AGENTS:
        raise ValueError("source must be a supported relay agent")
    if lane not in {"opener", "closer"}:
        raise ValueError("lane must be opener or closer")
    if fallback not in {"yes", "no"}:
        raise ValueError("fallback must be yes or no")
    cand_list = [part for part in candidates.split(",") if part]
    if not cand_list or chosen not in cand_list or not set(cand_list) <= _RELAY_PACING_AGENTS:
        raise ValueError("candidates must include chosen and only supported agents")
    for label, value in (
        ("weights", weights),
        ("states", states),
        ("candidates", candidates),
        ("chosen", chosen),
    ):
        if not value or any(char.isspace() for char in value):
            raise ValueError(f"{label} must be a nonempty no-whitespace token")
    token = decision_id if decision_id is not None else generate_relay_decision_id(when=when)
    if (
        not token
        or len(token) > feedback.RELAY_DECISION_ID_MAX_CHARS
        or any(char.isspace() for char in token)
    ):
        raise ValueError(
            "decision_id must be a nonempty token of at most "
            f"{feedback.RELAY_DECISION_ID_MAX_CHARS} characters"
        )
    stamp = (
        (when or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    line = (
        f"{stamp} decision_id={token} source={source} lane={lane} "
        f"candidates={candidates} chosen={chosen} weights={weights} "
        f"states={states} fallback={fallback}\n"
    )
    path = (
        log
        if log is not None
        else Path(os.environ.get("HANDOFF_PACING_LOG", str(HANDOFF / "relay-pacing.log")))
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
    return token


def record_lane_round(
    *,
    agent: str,
    surface: str = "handoff-relay",
    lane: str,
    receiver_reason: str | None,
    exit_status: int,
    output_file: Path,
    ts: int | None = None,
    relay_decision_id: str | None = None,
) -> dict[str, Any]:
    """Persist every relay exit and shed only on a nonzero authoritative failure."""
    import feedback

    if agent not in {"codex", "claude", "cursor", "muse"}:
        raise ValueError("agent must be codex, claude, cursor or muse")
    if not surface.strip():
        raise ValueError("surface must be nonempty")
    if not output_file.is_file() or output_file.stat().st_size > 128 * 1024:
        raise ValueError("output-file must be a readable tail of at most 128 KiB")
    evidence = "\n".join(output_file.read_text(errors="replace").splitlines()[-40:])
    timestamp = int(ts or time.time())
    category, subcategory, confidence = classify_provider_failure(evidence)
    authoritative = exit_status != 0 and confidence == "high"
    error_class = subcategory if authoritative else ("none" if exit_status == 0 else "unknown")
    decision_id = feedback.record_lane_round(
        agent,
        lane,
        receiver_reason,
        exit_status,
        error_class,
        timestamp,
        relay_decision_id=relay_decision_id,
    )
    # The installed relay already invokes this exit hook. Mirror sync activates the join
    # without editing a host-owned launcher. Telemetry failure must not hide a provider incident.
    try:
        pacing = feedback.ingest_relay_pacing(
            Path(os.environ.get("HANDOFF_PACING_LOG", HANDOFF / "relay-pacing.log")), now=timestamp
        )
    except (OSError, ValueError, sqlite3.Error) as exc:
        pacing = {"status": "unavailable", "reason": str(exc)}
    incident = None
    if authoritative:
        incident = record_incident(
            agent=agent,
            surface=surface,
            category=category,
            run_id=decision_id,
            evidence=evidence,
            timestamp=timestamp,
            reset_at=parse_relay_reset_at(evidence, now=timestamp),
            extra={"subcategory": subcategory, "lane": lane},
        )
    return {
        "decision_id": decision_id,
        "relay_pacing": pacing,
        "error_class": error_class,
        "incident_id": incident.get("incident_id") if incident else None,
        "shed": bool(incident and incident.get("shed")),
    }


def stdout_carries_capacity_evidence(text: str) -> bool:
    """Whether successful stdout is explicit enough to include in failure classification."""
    if not isinstance(text, str) or not text:
        return False
    return _contains_explicit_resource_exhausted(text) or bool(
        re.search(r"(?im)^\s*ActionRequiredError\b[^\n]*(?:out of usage|quota exhausted)", text)
    )


def json_event(line: str) -> dict | None:
    """The JSON object this log line holds, or None for any other line."""
    text = line.strip()
    if not text.startswith("{"):
        return None
    try:
        event = json.loads(text)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def is_codex_work_event(event: dict) -> bool:
    """A codex `exec --json` event that means the run DID something: the agent's own record of what
    it ran, read and said. An `error` item is the harness talking, not the agent working."""
    kind = event.get("type")
    if kind not in CODEX_WORK_EVENTS:
        return False
    item = event.get("item")
    return kind == "turn.completed" or not (isinstance(item, dict) and item.get("type") == "error")


def codex_agent_messages(lines: list[str]) -> list[str]:
    """What the agent SAID in a codex `exec --json` stream: the text of each completed
    `agent_message` item, in order. Everything else in the stream is what it ran and read, which can
    quote anything, or the harness talking. Empty when the agent said nothing.

    The one shape codex prints: 733 of 733 agent messages in the codex offload logs on 2026-10-05
    were an `item.completed` whose item is `{"type": "agent_message", "text": ...}`, none of them
    empty."""
    messages = []
    for line in lines:
        event = json_event(line) or {}
        item = event.get("item")
        if event.get("type") != "item.completed" or not isinstance(item, dict):
            continue
        text = item.get("text")
        if item.get("type") == "agent_message" and isinstance(text, str) and text.strip():
            messages.append(text)
    return messages


def failure_evidence(lines: list[str]) -> list[str]:
    """A failed run's output without the agent's own record of its work.

    A failed run's output is read as error evidence, but a codex `exec --json` transcript is also
    every command the agent ran and every file it read: a run that `sed`s a doc about quotas carries
    "quota exhausted" in an `item.completed` event. On 2026-10-04, 11 of the 64 codex dispatch
    transcripts did, with no refusal anywhere, and two codex offloads (2026-09-19, 09-20) that had
    printed such a doc were recorded as quota incidents, each shedding the seat. Codex's own failure
    evidence is its harness events (`turn.failed`, `error`) and its stderr lines, which stay. A
    text-only log has no such structure and stays whole.

    One predicate for every reader of a failed run: the completion reconciler's log segment
    (`ledger_reconcile`) and a synchronous run's stdout (`failed_stdout_evidence`). Both observers of
    a run share one incident (`_idempotency_key`), so a rule only one of them applied would let the
    other record the incident the first had refused.
    """
    return [line for line in lines if not is_codex_work_event(json_event(line) or {})]


def failed_stdout_evidence(agent: str, stdout: str) -> str:
    """The part of a FAILED synchronous run's stdout that may be read as provider evidence.

    codex prints its `exec --json` stream on stdout in every mode but `assess`, so its stdout is read
    less the agent's own work events (`failure_evidence`). Every other agent's stdout is its final
    text, which has no such structure, and is read whole by design. A successful run's stdout is not
    this function's to read: it counts only through `stdout_carries_capacity_evidence`.
    """
    if agent != "codex":
        return stdout
    return "\n".join(failure_evidence(stdout.splitlines()))


def _harness_error_messages(event: dict) -> list[str]:
    """The text of one codex harness error event (`error`, `turn.failed`, or an error item)."""
    kind = event.get("type")
    if kind == "error":
        holder: object = event
    elif kind == "turn.failed":
        holder = event.get("error")
    else:
        holder = event.get("item")
        if not (isinstance(holder, dict) and holder.get("type") == "error"):
            return []
    if isinstance(holder, str):
        return [holder]
    message = holder.get("message") if isinstance(holder, dict) else None
    return [message] if isinstance(message, str) else []


def parse_resets_in_duration(text: str, *, now: int | None = None) -> int | None:
    """Parse provider copy like ``Resets in 140h5m6s`` into an absolute unix time."""
    match = re.search(
        r"Resets\s+in\s+(\d+)h(\d+)m(\d+)s",
        text,
        re.I,
    )
    if not match:
        return None
    hours, minutes, seconds = (int(match.group(i)) for i in range(1, 4))
    base = now if now is not None else int(time.time())
    return base + hours * 3600 + minutes * 60 + seconds


def provider_reset_at(agent: str, lines: list[str], *, now: int | None = None) -> int | None:
    """When the provider says it will serve again, or None for the default cooldown.

    Codex: the reset is the provider's clock ("try again at Sep 28th, 2026 8:26 PM"), and it may
    come only from the provider's text: the messages of the harness's `error` and `turn.failed`
    events among the lines `failure_evidence` keeps. A plain line is never read for codex. A codex
    run without `--json` (`assess`) prints its transcript as text, and stderr can carry the agent's
    commands too, so a plain line may be the agent's own work, and a reset read from it would hold
    the seat until whatever date that work happened to mention.

    Gemini: output is plain text (glog-prefixed agent-executor lines). Parse ``Resets in NhNmNs``
    from those lines so shed lasts until the provider's stated window rather than DEFAULT_COOLDOWN_S.
    Every other agent's answer is None, and the seat sheds for the cooldown.

    One reading for every observer of a run: the synchronous offload, `adapters.dispatch` and the
    completion reconciler. They share one incident (`_idempotency_key`), and the first to record it
    sets the shed, so a reading only one of them made would decide by which one ran first. Until
    2026-10-05 the offload passed no reset at all: the two codex refusals of 2026-09-25
    (offload:codex:1790322148167312000 and 1790322306091352000) named Sep 28 and shed for the 6 h
    cooldown. Several stated resets in one run: the latest is the one that binds.
    """
    if agent == "gemini":
        resets = []
        for line in failure_evidence(lines):
            if not _has_provider_error_context(line) or not is_authoritative_error(line):
                continue
            reset = parse_resets_in_duration(line, now=now)
            if reset is not None:
                resets.append(reset)
        return max(resets) if resets else None
    if agent != "codex":
        return None
    resets = []
    for line in failure_evidence(lines):
        for message in _harness_error_messages(json_event(line) or {}):
            reset = parse_relay_reset_at(message, now=now)
            if reset is not None:
                resets.append(reset)
    return max(resets) if resets else None


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
    agent: str,
    *,
    category: str,
    incident_id: str,
    expires_at: int | None = None,
    reset_at: int | None = None,
) -> bool:
    """Write or extend one JSON marker without shortening an active shed."""
    _ensure_paths()
    marker = SHED_DIR / agent
    lock_fd = _acquire_lock()
    temporary: Path | None = None
    try:
        if marker.exists():
            try:
                existing = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                # Legacy empty or malformed markers remain manual, authoritative sheds.
                return True
            existing_expiry = existing.get("expires_at") if isinstance(existing, dict) else None
            if (
                isinstance(existing_expiry, bool)
                or not isinstance(existing_expiry, (int, float))
                or not math.isfinite(existing_expiry)
            ):
                return True
            if existing_expiry > time.time() and (
                expires_at is None or expires_at <= existing_expiry
            ):
                return True
        payload = {"created_at": int(time.time()), "category": category, "incident_id": incident_id}
        if expires_at is not None:
            payload["expires_at"] = expires_at
        if reset_at is not None:
            payload["reset_at"] = reset_at
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=SHED_DIR, prefix=f".{agent}.", delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(json.dumps(payload, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, marker)
            temporary = None
            return True
        except OSError:
            return False
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        _release_lock(lock_fd)


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
    expires_at = timestamp + cooldown if cooldown is not None else None
    if reset_at is not None and expires_at is not None:
        expires_at = max(expires_at, reset_at)
    shed_created = (
        ensure_shed(
            agent,
            category=category,
            incident_id=incident_id,
            expires_at=expires_at,
            reset_at=reset_at,
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
        stream = [
            '{"type":"item.completed","item":{"type":"command_execution",'
            '"aggregated_output":"OFFLOAD_INCOMPLETE: quoted by a doc"}}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"Report staged."}}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"  "}}',
            "not an event",
        ]
        assert codex_agent_messages(stream) == ["Report staged."]
        assert codex_agent_messages(["not an event"]) == []
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
        # Codex: reset from harness error events only, never a plain line.
        refusal = "You've hit your usage limit. Try again at Jan 5th, 2099 3:11 AM."
        event = json.dumps({"type": "turn.failed", "error": {"message": refusal}})
        assert provider_reset_at("codex", [event]) == parse_relay_reset_at(refusal) is not None
        assert provider_reset_at("codex", [refusal]) is None
        assert provider_reset_at("claude", [event]) is None
        # Gemini: glog agent-executor Individual quota line is authoritative quota + Resets-in.
        gemini_line = (
            "E1018 01:42:33.766065 697 errorreport.go:224] agent executor error: generating and "
            "executing: RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade "
            "your subscription to increase your limits. Resets in 140h5m6s."
        )
        assert classify_provider_failure(gemini_line) == ("quota", "quota_exhausted", "high")
        assert not is_authoritative_error("handle RESOURCE_EXHAUSTED errors")
        for prose in ("handle RESOURCE_EXHAUSTED (code 429) errors", "handle quota reached errors"):
            assert not is_authoritative_error(prose)
            assert not stdout_carries_capacity_evidence(prose)
        base = 1_700_000_000
        assert (
            provider_reset_at("gemini", [gemini_line], now=base) == base + 140 * 3600 + 5 * 60 + 6
        )
        assert provider_reset_at(
            "gemini", ["Document Resets in 999h0m0s", gemini_line], now=base
        ) == provider_reset_at("gemini", [gemini_line], now=base)
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
    lane_parser = subparsers.add_parser(
        "record-lane-round", help="record a detached handoff relay round"
    )
    lane_parser.add_argument(
        "--agent", choices=("codex", "claude", "cursor", "muse"), required=True
    )
    lane_parser.add_argument("--surface", required=True)
    lane_parser.add_argument("--lane", choices=("opener", "closer"), required=True)
    lane_parser.add_argument("--receiver-reason")
    lane_parser.add_argument("--exit", dest="exit_status", type=int, required=True)
    lane_parser.add_argument("--output-file", type=Path, required=True)
    lane_parser.add_argument("--ts", type=int)
    lane_parser.add_argument("--relay-decision-id")
    subparsers.add_parser(
        "generate-relay-decision-id",
        help="print one <utc>-<pid>-<random> token for the relay pacing log",
    )
    append_parser = subparsers.add_parser(
        "append-relay-pacing-decision",
        help="append one decision_id= pacing log line; print the token",
    )
    append_parser.add_argument("--source", required=True)
    append_parser.add_argument("--lane", choices=("opener", "closer"), required=True)
    append_parser.add_argument("--candidates", required=True)
    append_parser.add_argument("--chosen", required=True)
    append_parser.add_argument("--weights", required=True)
    append_parser.add_argument("--states", required=True)
    append_parser.add_argument("--fallback", choices=("yes", "no"), required=True)
    append_parser.add_argument("--decision-id")
    append_parser.add_argument("--log", type=Path)
    append_parser.add_argument("--ts", type=int, help="UTC epoch second for the log stamp")
    format_parser = subparsers.add_parser(
        "format-relay-pacing-fields",
        help="turn pick-agent decision JSON (stdin) into weights=/states= log tokens",
    )
    format_parser.add_argument("--candidates", required=True)
    subparsers.add_parser("summary", help="summarize the append-only incident ledger")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    if args.command == "generate-relay-decision-id":
        print(generate_relay_decision_id())
        return 0
    if args.command == "format-relay-pacing-fields":
        try:
            payload = json.loads(sys.stdin.read())
            if not isinstance(payload, dict):
                raise ValueError("decision JSON must be an object")
            print(json.dumps(relay_log_fields_from_decision(payload, candidates=args.candidates)))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            parser.error(str(exc))
        return 0
    if args.command == "append-relay-pacing-decision":
        try:
            when = datetime.fromtimestamp(args.ts, timezone.utc) if args.ts is not None else None
            token = append_relay_pacing_decision(
                source=args.source,
                lane=args.lane,
                candidates=args.candidates,
                chosen=args.chosen,
                weights=args.weights,
                states=args.states,
                fallback=args.fallback,
                decision_id=args.decision_id,
                log=args.log,
                when=when,
            )
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        print(token)
        return 0
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
    if args.command == "record-lane-round":
        try:
            result = record_lane_round(
                agent=args.agent,
                surface=args.surface,
                lane=args.lane,
                receiver_reason=args.receiver_reason,
                exit_status=args.exit_status,
                output_file=args.output_file,
                ts=args.ts,
                relay_decision_id=args.relay_decision_id,
            )
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
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
