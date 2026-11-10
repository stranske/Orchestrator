#!/usr/bin/env python3
"""Collect the two finite delayed-evidence follow-ups without running agents or experiments.

The existing hourly tick owns collection. Reports are local, source-hashed and
fail closed. Only a newly complete evidence packet revives its existing opener;
this module never admits Brain costs, changes acceptance, evicts a writer or closes
an issue. Native/export changes are watched locally; unchanged UNKNOWNs use no API.
"""

from __future__ import annotations

import argparse
import collections
import fcntl
import hashlib
import json
import math
import os
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from paths import runtime_state_path

EXPERIMENT = "strategy434-20261006"
EXCLUDED = "stranske/" + "Collab-Admin"
MAX_COLLECTION_GAP = timedelta(hours=2)


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def document(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(document(value))
        handle.flush()
        os.fsync(handle.fileno())
        temporary = handle.name
    os.replace(temporary, path)


def load(path: Path, default=None):
    return json.loads(path.read_bytes()) if path.exists() else default


def when(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone required")
    return result


def eligible_lines(path: Path) -> list[bytes]:
    # Owner exclusion applies before parsing, counting, persisting or reporting.
    return [
        raw
        for raw in path.read_bytes().splitlines(keepends=True)
        if EXCLUDED.lower().encode() not in raw.lower()
    ]


def provision_report(root: Path, state: dict, now: datetime, instrument: Path) -> tuple[dict, dict]:
    files = {
        str(path): eligible_lines(path)
        for path in sorted(root.glob("provision-events.ndjson*"))
        if path.is_file()
    }
    failures = []
    counts: collections.Counter[tuple[str, str]] = collections.Counter()
    latest: dict[str, dict] = {}
    cutoff = now - timedelta(days=7)
    for path, lines in files.items():
        for line in lines:
            try:
                item = json.loads(line)
                ts = when(item["ts"])
                if (
                    not line.endswith(b"\n")
                    or not isinstance(item["outcome"], str)
                    or not isinstance(item["cause"], str)
                ):
                    raise ValueError("incomplete ledger line")
            except (ValueError, KeyError, TypeError):
                failures.append(f"malformed or partial segment: {path}")
                continue
            if cutoff <= ts <= now:
                counts[(item["outcome"], item["cause"])] += 1
                if item["outcome"] == "failed" and item["cause"] in {
                    "branch-held",
                    "branch-exists",
                }:
                    key = item.get("target", "unknown")
                    if ts > when(latest.get(key, {"ts": cutoff.isoformat()})["ts"]):
                        latest[key] = item
    source = digest(instrument.read_bytes())
    collector_source = digest(Path(__file__).read_bytes())
    if not files:
        failures.append("no ledger")
    if state:
        gap = now - when(state["checked_at"])
        if gap < timedelta(0) or gap > MAX_COLLECTION_GAP:
            failures.append("collector continuity gap exceeds two hours")
        if collector_source != state.get("collector_sha256"):
            failures.append("collector implementation changed; new baseline required")
        if source != state.get("instrument_sha256"):
            failures.append("provision instrument changed; new baseline required")
        # A rename is acceptable only if an intact prior prefix still exists in
        # an active/archive segment. Never deduplicate events from the count.
        available = set(files)
        for old in sorted(state.get("segments", []), key=lambda row: row["lines"], reverse=True):
            matches = [
                path
                for path in available
                if len(files[path]) >= old["lines"]
                and digest(b"".join(files[path][: old["lines"]])) == old["sha256"]
            ]
            if not matches:
                failures.append("previous ledger prefix missing or rewritten")
                break
            # Multiplicity matters: two identical segments cannot both be
            # accounted for by one retained file after a deletion.
            available.remove(old["path"] if old["path"] in matches else matches[0])
    start = (
        state.get("continuous_since", now.isoformat())
        if state and not failures
        else now.isoformat()
    )
    segments = [
        {"path": path, "lines": len(lines), "sha256": digest(b"".join(lines))}
        for path, lines in files.items()
    ]
    next_state = {
        "checked_at": now.isoformat(),
        "continuous_since": start,
        "instrument_sha256": source,
        "collector_sha256": collector_source,
        "segments": segments,
    }
    coverage = not failures and now - when(start) >= timedelta(days=7)
    refused = sum(counts[("failed", cause)] for cause in ("branch-held", "branch-exists"))
    report = {
        "issue": 1098,
        "window_start": cutoff.isoformat(),
        "checked_at": now.isoformat(),
        "coverage": "continuous" if coverage else "UNKNOWN",
        "continuous_since": start,
        "collection_max_gap_seconds": MAX_COLLECTION_GAP.total_seconds(),
        "continuity_failures": failures,
        "instrument_sha256": source,
        "collector_sha256": collector_source,
        "segments": segments,
        "counts": {f"{outcome}/{cause}": n for (outcome, cause), n in sorted(counts.items())},
        "branch_refusals": refused,
        "latest_refusals": list(latest.values()),
        "evidence_ready": coverage and refused == 0,
        "blocking": 1,
        "drainable": 1,
        "drain": "existing hourly collector retains intact segments; existing opener verifies full acceptance after a genuine seven-day zero-refusal window",
    }
    return report, next_state


def usage_receipt(raw: dict, expected: dict, export: Path) -> dict:
    """Validate an original whole-attempt export; never accept placeholder counters."""
    run_id = expected["run_id"]
    if (
        raw.get("schema") != "original-attempt-usage/v1"
        or raw.get("experiment_id") != EXPERIMENT
        or raw.get("attempt_run_id") != run_id
        or raw.get("session_id") != expected["session_id"]
        or raw.get("terminal") is not True
        or raw.get("coverage") != "whole_attempt"
        or raw.get("source_kind") not in {"provider_export", "native_terminal_usage"}
    ):
        raise ValueError("original run/session/terminal/coverage binding missing")
    if raw.get("provider") != expected["provider"]:
        raise ValueError("provider binding mismatch")
    for key in ("started_at", "completed_at"):
        if when(raw[key]) != when(expected[key]):
            raise ValueError("original attempt time binding mismatch")
    source = Path(raw["raw_source_path"]).resolve()
    # Exports are confined to the inbox. No arbitrary path or credential reads.
    if not source.is_relative_to(export.parent.resolve()) or source == export.resolve():
        raise ValueError("raw source must be a separate export in the inbox")
    if digest(source.read_bytes()) != raw["raw_source_sha256"]:
        raise ValueError("raw export hash mismatch")
    original = json.loads(source.read_bytes())
    # Exact record equality makes the normalization auditable and prevents a
    # receipt attaching arbitrary usage to an unrelated hashed export.
    records = original if isinstance(original, list) else [original]
    fields = (
        "attempt_run_id",
        "session_id",
        "provider",
        "terminal",
        "coverage",
        "started_at",
        "completed_at",
        "segments",
    )
    if not any(
        all(record.get(k) == raw.get(k) for k in fields)
        for record in records
        if isinstance(record, dict)
    ):
        raise ValueError("raw provider record does not match normalized receipt")
    cost = 0.0
    segments = raw.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("per-model/cache usage and pricing missing")
    seen = set()
    for segment in segments:
        if (
            not segment.get("model")
            or not segment.get("request_id")
            or segment["request_id"] in seen
        ):
            raise ValueError("unique original request/model binding required")
        seen.add(segment["request_id"])
        if not segment.get("price_source_url", "").startswith("https://"):
            raise ValueError("applicable price source required")
        if (
            not when(segment["price_valid_from"])
            <= when(raw["started_at"])
            < when(segment["price_valid_until"])
        ):
            raise ValueError("price not applicable to original date")
        for kind in ("input", "output", "cache_read", "cache_write"):
            tokens = segment.get(kind + "_tokens")
            price = segment.get(kind + "_usd_per_million")
            if (
                type(tokens) is not int
                or tokens < 0
                or type(price) not in (int, float)
                or not math.isfinite(price)
                or price < 0
            ):
                raise ValueError(f"explicit {kind} usage and price required")
            cost += tokens * price / 1_000_000
    return {
        "attempt_run_id": run_id,
        "source": "original_provider_export_candidate",
        "cost_scale": "list_price_usd",
        "cost_usd": cost,
        "receipt_sha256": digest(export.read_bytes()),
        "raw_source_sha256": raw["raw_source_sha256"],
        "brain_admitted": False,
    }


def strategy_report(config: dict, inbox: Path) -> dict:
    attempts, errors, qualified = [], [], []
    for item in config["attempts"]:
        done = Path(item["done_path"])
        terminal = load(done, {})
        binding = Path(item["binding_path"])
        valid_binding = binding.is_file() and digest(binding.read_bytes()) == item["binding_sha256"]
        valid_done = (
            (
                valid_binding
                and digest(done.read_bytes()) == item["done_sha256"]
                and terminal.get("run_id") == item["run_id"]
                and terminal.get("rc") == 0
                and terminal.get("rc_of") == "agent"
            )
            if done.exists()
            else False
        )
        candidates = []
        for export in sorted(inbox.glob("*.receipt.json")):
            try:
                raw = load(export)
                if not isinstance(raw, dict):
                    raise ValueError("export receipt must be an object")
                if raw.get("attempt_run_id") != item["run_id"]:
                    continue
                if not valid_done:
                    raise ValueError("original terminal artifact missing or changed")
                candidates.append(usage_receipt(raw, item, export))
            except (ValueError, KeyError, TypeError, OSError) as exc:
                errors.append(
                    {"file": export.name, "attempt_run_id": item["run_id"], "reason": str(exc)}
                )
        # Conflicting receipts are UNKNOWN; no last-write-wins cost overwrite.
        if len(candidates) == 1:
            qualified.extend(candidates)
        attempts.append(
            {
                "run_id": item["run_id"],
                "terminal_bound": valid_done,
                "qualified_exports": len(candidates),
                "missing": (
                    []
                    if len(candidates) == 1
                    else [
                        "authentic whole-attempt provider usage, session binding, cache-aware applicable pricing"
                    ]
                ),
            }
        )
    identities = {item["run_id"] for item in config["attempts"]}
    ready = len(identities) == 9 and len(qualified) == 9 and not errors
    watched = []
    for name in config.get("watch_paths", []):
        path = Path(name)
        watched.append(
            {"path": name, "sha256": digest(path.read_bytes()) if path.is_file() else None}
        )
    return {
        "issue": 434,
        "experiment_id": EXPERIMENT,
        "qualified_original_attempts": len(qualified),
        "expected_original_attempts": 9,
        "attempts": attempts,
        "receipts": qualified,
        "errors": errors,
        "watched_sources": watched,
        "evidence_ready": ready,
        "original_acceptance_complete": False,
        "brain_admitted": False,
        "blocking": 1,
        "drainable": 1,
        "drain": "watch exact original sources and provider-export inbox; revive existing opener once all nine validate, retaining synthesis and source-admission gates",
    }


def gh(method: str, path: str, payload: dict | None = None):
    args = ["gh", "api", "--method", method, path]
    result = subprocess.run(
        args + (["--input", "-"] if payload is not None else []),
        input=json.dumps(payload) if payload is not None else None,
        capture_output=True,
        text=True,
        timeout=45,
        check=True,
    )
    return json.loads(result.stdout) if result.stdout.strip() else None


def reconcile_ready(report: dict, state_dir: Path) -> dict:
    """Idempotent comment readback precedes reviving the existing native opener."""
    number = report["issue"]
    ack = state_dir / f"ack-{number}.json"
    previous = load(ack, {})
    if previous.get("state") == "acknowledged":
        return previous
    # A later incomplete collection cannot strand a previous uncertain remote
    # publication. Reconcile its frozen packet before considering new readiness.
    if not report["evidence_ready"] and not previous.get("evidence_sha256"):
        return {"action": "retain_incomplete", "network_calls": 0}
    payload_hash = digest(document(report))
    if previous.get("evidence_sha256"):
        # Resume the frozen publication, even if another local collection has
        # changed counts/paths while the previous remote outcome was unknown.
        payload_hash = previous["evidence_sha256"]
        report = load(state_dir / f"ready-{number}-{payload_hash}.json")
        if not report or digest(document(report)) != payload_hash:
            raise ValueError("pending acknowledgment lost its frozen source packet")
    else:
        save(state_dir / f"ready-{number}-{payload_hash}.json", report)
        save(ack, {"state": "pending", "evidence_sha256": payload_hash})
    root = f"repos/stranske/Orchestrator/issues/{number}"
    marker = f"<!-- followup-evidence/v1 issue={number} sha256={payload_hash} -->"
    comments = []
    for page in range(1, 101):
        batch = gh("GET", f"{root}/comments?per_page=100&page={page}")
        comments.extend(batch)
        if len(batch) < 100:
            break
    else:
        raise RuntimeError("incomplete comment pagination")
    if not any(marker in item.get("body", "") for item in comments):
        response = gh(
            "POST",
            f"{root}/comments",
            {
                "body": marker
                + "\nOriginal delayed evidence now qualifies for the remaining acceptance run. "
                "Source-hashed packet retained by the existing hourly collector. This does not close the issue, admit Brain costs, "
                "or waive native/full-verifier, synthesis, CI or review gates.\n```json\n"
                + document(report).decode()
                + "```"
            },
        )
        # Readback an accepted write before acknowledging it; unknown writes stay retryable by marker.
        observed = gh("GET", f"repos/stranske/Orchestrator/issues/comments/{response['id']}")
        if marker not in observed.get("body", ""):
            raise RuntimeError("completion evidence publication readback mismatch")
    save(state_dir / f"ready-{number}-{payload_hash}.json", report)
    save(ack, {"state": "published", "evidence_sha256": payload_hash})
    issue = gh("GET", root)
    if issue.get("state") == "open" and any(
        label["name"] == "agents:paused" for label in issue.get("labels", [])
    ):
        gh("DELETE", f"{root}/labels/{quote('agents:paused', safe='')}")
    issue = gh("GET", root)
    if issue.get("state") == "open" and any(
        label["name"] == "agents:paused" for label in issue.get("labels", [])
    ):
        raise RuntimeError("existing opener revival not confirmed")
    result = {
        "state": "acknowledged",
        "evidence_sha256": payload_hash,
        "owner": "existing opener-core",
        "issue_state": issue["state"],
    }
    save(ack, result)
    return result


def collect(root: Path, config_path: Path, *, apply: bool = False) -> dict:
    state_dir = root / "followup-evidence"
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "collector.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"action": "existing_collector_running", "blocking": 0, "drainable": 0}
        config = load(config_path)
        if not config:
            raise ValueError("original-source config missing; no inferred replacement experiment")
        now = datetime.now(timezone.utc)
        provision, next_state = provision_report(
            root,
            load(state_dir / "provision-state.json", {}),
            now,
            Path(__file__).with_name("provision.py"),
        )
        strategy = strategy_report(config, Path(config["export_inbox"]))
        report = {
            "checked_at": now.isoformat(),
            "provision": provision,
            "strategy": strategy,
            "collector_source_sha256": digest(Path(__file__).read_bytes()),
        }
        save(state_dir / "provision-state.json", next_state)
        # Stable reconciliation identities exclude the hourly checked-at value.
        for packet in (provision, strategy):
            if apply:
                stable = {
                    k: v
                    for k, v in packet.items()
                    if k not in {"checked_at", "window_start", "segments"}
                }
                packet["continuation"] = reconcile_ready(stable, state_dir)
        save(state_dir / "latest.json", report)
        return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=runtime_state_path())
    parser.add_argument("--config", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    result = collect(
        args.state_dir,
        args.config or args.state_dir / "followup-evidence/config.json",
        apply=args.apply,
    )
    print(
        json.dumps(
            {
                "outcome2": result.get("provision", {}).get("coverage"),
                "branch_refusals": result.get("provision", {}).get("branch_refusals"),
                "outcome3": result.get("strategy", {}).get("qualified_original_attempts"),
                "result": str(args.state_dir / "followup-evidence/latest.json"),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
