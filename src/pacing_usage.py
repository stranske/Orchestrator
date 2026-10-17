"""Local pacing evidence I/O. No credentials, browser scraping or provider calls.

Confirmed Claude desktop source: ~/Library/Application Support/Claude/plan-usage-history.json
(samples: t milliseconds, u.sd weekly percent, u.fh five-hour percent). Refresh is
app-driven, not guaranteed; use each record's t, never the file modification time.
Codex source: ~/.codex/sessions/**/rollout-*.jsonl token_count events,
rate_limits.primary weekly percent and reset epoch. Credits are never extracted.
Fallback: agent-specific weekly-*.json plus timestamp-matched quota calibration.
Mixed-agent ccusage totals are never silently attributed to Codex or Claude.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
from pathlib import Path

import pacing

STATE_DIR = Path(os.environ.get("ORCH_STATE_DIR", Path.home() / ".codex/orchestrator"))
RESETS_FILE = STATE_DIR / "capacity-resets.json"
CALIBRATION_FILE = STATE_DIR / "capacity-calibration.json"
CLAUDE_USAGE = Path.home() / "Library/Application Support/Claude/plan-usage-history.json"
SNAPSHOTS = Path.home() / ".codex/usage-snapshots"
CODEX_SESSIONS = Path.home() / ".codex/sessions"
ROLLOUT_FILE_LIMIT = 300


def read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def read_rows(path: Path) -> list[dict]:
    rows = []
    bad = 0
    try:
        with path.open() as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    if isinstance(row, dict):
                        rows.append(row)
                    else:
                        bad += 1
                except ValueError:
                    bad += 1
    except FileNotFoundError:
        return []
    if bad:
        print(f"FYI {path.name}: {bad} malformed rows omitted", file=sys.stderr)
    return rows


def append_rows(path: Path, rows: list[dict]) -> None:
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        for row in rows:
            stream.write(json.dumps(row, allow_nan=False) + "\n")
        stream.flush()
        fcntl.flock(stream, fcntl.LOCK_UN)


def reset_inventory(path: Path, now: float) -> tuple[dict, list[str]]:
    data = read_json(path)
    if not isinstance(data, dict):
        return {}, ["FYI resets=unknown: capacity-resets.json absent or unreadable"]
    messages = []
    for pool in ("codex", "claude"):
        entries = data.get(pool)
        if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
            data.pop(pool, None)
            messages.append(f"FYI {pool}: resets=unknown; invalid or missing inventory")
            continue
        if any(pacing.timestamp(e.get("expires")) is None for e in entries):
            data.pop(pool, None)
            messages.append(f"FYI {pool}: resets=unknown; invalid expiry")
            continue
        for i, entry in enumerate(entries, 1):
            if entry.get("consumed_at"):
                messages.append(
                    f"FYI {pool}: reset {entry.get('id', i)} consumed at {entry['consumed_at']}"
                )
            elif (expiry := pacing.timestamp(entry["expires"])) is not None and expiry <= now:
                messages.append(
                    f"FYI {pool}: reset {entry.get('id', i)} expired {entry['expires']}"
                )
            else:
                messages.append(
                    f"FYI {pool}: reset {entry.get('id', i)} expires {entry['expires']}; "
                    "owner-supplied expiry UNKNOWN (30-day estimate)"
                )
    return data, messages


def weekly_tokens(path: Path, pool: str) -> list[dict]:
    data = read_json(path)
    if not isinstance(data, dict):
        return []
    rows = []
    for row in data.get("weekly", []):
        agents = (row.get("metadata") or {}).get("agents")
        if row.get("agent") != pool and agents != [pool]:
            continue
        start = pacing.timestamp(row.get("period"))
        tokens = row.get("totalTokens")
        if start is not None and isinstance(tokens, (int, float)) and tokens >= 0:
            rows.append(dict(period_start=start, tokens=tokens))
    return rows


def rollout_observations(root: Path, now: float) -> list[dict]:
    """Read only an allowlisted weekly payload. Never retain credits or raw events."""
    files = sorted(root.glob("**/rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    omitted = max(0, len(files) - ROLLOUT_FILE_LIMIT)
    if omitted:
        print(
            f"FYI codex rollouts: scanning {len(files)-omitted}/{len(files)} newest files; "
            f"omitted={omitted} older files",
            file=sys.stderr,
        )
    rows = []
    malformed = unreadable = 0
    for path in files[:ROLLOUT_FILE_LIMIT]:
        try:
            with path.open() as stream:
                for line in stream:
                    if '"token_count"' not in line or '"rate_limits"' not in line:
                        continue
                    try:
                        event = json.loads(line)
                        payload = event.get("payload") or {}
                        primary = (payload.get("rate_limits") or {}).get("primary") or {}
                        ts = pacing.timestamp(event.get("timestamp"))
                        percent = primary.get("used_percent")
                        end = pacing.timestamp(primary.get("resets_at"))
                        if (
                            event.get("type") == "event_msg"
                            and payload.get("type") == "token_count"
                            and primary.get("window_minutes") == 10080
                            and ts is not None
                            and ts <= now
                            and end is not None
                            and isinstance(percent, (int, float))
                            and 0 <= percent <= 100
                        ):
                            rows.append(
                                dict(
                                    ts=ts,
                                    used_fraction=percent / 100,
                                    period_end=end,
                                    source="codex-rollout:rate_limits.primary",
                                )
                            )
                    except (ValueError, TypeError, AttributeError):
                        malformed += 1
        except OSError:
            unreadable += 1
    if malformed or unreadable:
        print(
            f"FYI codex rollouts: malformed={malformed}, unreadable_files={unreadable}",
            file=sys.stderr,
        )
    return sorted(rows, key=lambda r: r["ts"])


def codex_rollout_usage(root: Path, now: float) -> dict | None:
    rows = rollout_observations(root, now)
    if not rows:
        return None
    resets = []
    signals = []
    for left, right in zip(rows, rows[1:]):
        dropped = right["used_fraction"] < left["used_fraction"]
        shift = right["period_end"] - left["period_end"]
        if right["ts"] > left["ts"] and (abs(shift) > 1 or dropped):
            signals.append(right["ts"])
            # Real concurrent rollouts contain percent reversals and epoch jitter.
            # An early rollover needs BOTH independent signals; ordinary scheduled
            # rollover grants a new period without spending a bank entry.
            early = right["period_end"] - pacing.PERIOD_SECONDS < left["period_end"] - 1
            if dropped and shift > pacing.TICK_SECONDS and early:
                resets.append(right["ts"])
    return dict(rows[-1], reset_events=sorted(set(resets)), reset_signals=sorted(set(signals)))


def consume_detected_resets(inventory: dict, datum: dict, *, now: float, record: bool) -> list[str]:
    """Idempotently debit oldest-expiring bank entries, preserving the audit trail."""
    entries = inventory.get("codex")
    if not isinstance(entries, list):
        return []
    messages = [
        f"FYI codex: reset signals={len(datum.get('reset_signals', []))}; "
        f"confirmed bank consumptions={len(datum.get('reset_events', []))}; "
        "auto-debit requires usage drop + >one-tick advance of T1 before scheduled rollover"
    ]
    baseline = pacing.timestamp(inventory.get("observed_at")) or 0
    consumed = {pacing.timestamp(e.get("consumed_at")) for e in entries}
    for ts in datum.get("reset_events", []):
        if ts <= baseline or ts in consumed:
            continue
        bank = sorted(
            pacing.active_resets(entries, ts), key=lambda e: pacing.timestamp(e["expires"]) or 0
        )
        if not bank:
            messages.append(
                f"FYI codex: reset detected at {ts:.0f}; no recorded bank entry available"
            )
            continue
        bank[0]["consumed_at"] = ts
        consumed.add(ts)
        messages.append(
            f"FYI codex: auto-consumed oldest-expiring reset at {ts:.0f}; expires={bank[0]['expires']}"
        )
    if record and datum.get("ts") is not None:
        inventory["observed_at"] = max(baseline, datum["ts"])
        temporary = RESETS_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(inventory, indent=2) + "\n")
        temporary.replace(RESETS_FILE)
    return messages


def usage(
    pool: str,
    now: float,
    calibration: dict,
    snapshots: Path = SNAPSHOTS,
    claude_path: Path = CLAUDE_USAGE,
    sessions: Path | None = None,
) -> dict:
    if pool == "codex":
        observed = codex_rollout_usage(CODEX_SESSIONS if sessions is None else sessions, now)
        if observed is not None:
            return observed
    if pool == "claude":
        data = read_json(claude_path) or {}
        rows = data.get("samples", [])
        valid = [
            r
            for r in rows
            if isinstance(r, dict)
            and isinstance(r.get("t"), (int, float))
            and isinstance(r.get("u"), dict)
            and isinstance(r["u"].get("sd"), (int, float))
        ]
        if valid:
            row = max(valid, key=lambda r: r["t"])
            ts = row["t"] / 1000
            if 0 <= row["u"]["sd"] <= 100 and ts <= now:
                return dict(
                    used_fraction=row["u"]["sd"] / 100,
                    ts=ts,
                    source="claude-desktop:plan-usage-history.json:u.sd",
                )
    cap = calibration.get(f"{pool}_weekly_token_cap")
    paths = sorted(snapshots.glob("weekly-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths:
        rows = weekly_tokens(path, pool)
        if not rows:
            continue
        row = max(rows, key=lambda r: r["period_start"])
        if not isinstance(cap, (int, float)) or cap <= 0:
            return dict(
                used_fraction=None,
                ts=path.stat().st_mtime,
                source="ccusage-unscaled:weekly-agent-tokens",
                weekly_tokens=row["tokens"],
                period_start=row["period_start"],
            )
        # Calendar-week aggregates cannot measure a bank-reset-shifted week.
        bounds = pacing.period(pool, now, start=calibration.get("codex_period_start"))
        if bounds is None or abs(row["period_start"] - bounds[0]) > 1:
            continue
        return dict(
            used_fraction=row["tokens"] / cap,
            ts=path.stat().st_mtime,
            source="ccusage-calibrated:weekly-agent-tokens",
            weekly_tokens=row["tokens"],
            period_start=row["period_start"],
        )
    return dict(used_fraction=None, ts=None, source="none")


def calibrate(incidents: list[dict], samples: list[dict], destination: Path) -> tuple[bool, str]:
    evidence = []
    seen = set()
    counts = dict(seen=0, unmatched=0, duplicate=0, no_weekly_tokens=0)
    for incident in incidents:
        if incident.get("agent") != "codex" or incident.get("category") != "quota":
            continue
        counts["seen"] += 1
        ts = pacing.timestamp(incident.get("ts"))
        key = incident.get("incident_id") or incident.get("evidence_hash")
        if key in seen:
            counts["duplicate"] += 1
            continue
        if ts is None or not key:
            counts["unmatched"] += 1
            continue
        seen.add(key)
        extra = incident.get("extra") or {}
        tokens = extra.get("weekly_tokens")
        start = pacing.timestamp(extra.get("period_start"))
        if tokens is None:
            matches = [
                r
                for r in samples
                if r.get("agent") == "codex"
                and r.get("source", "").startswith("ccusage")
                and abs(r.get("ts", 0) - ts) <= pacing.STALE_AFTER_SECONDS
                and r.get("weekly_tokens") is not None
            ]
            if matches:
                sample = min(matches, key=lambda r: abs(r["ts"] - ts))
                tokens, start = sample["weekly_tokens"], sample.get("period_start")
        if not isinstance(tokens, (int, float)) or tokens <= 0 or start is None:
            counts["no_weekly_tokens"] += 1
            counts["unmatched"] += 1
            continue
        evidence.append(dict(incident_id=key, ts=ts, weekly_tokens=tokens, period_start=start))
    if len(evidence) < 2:
        return False, (
            f"calibrate-codex: refuses to write: {len(evidence)} matched quota incidents; "
            + ", ".join(f"{k}={v}" for k, v in counts.items())
            + "; "
            "need at least 2 distinct incidents with agent-specific weekly token projections "
            "and period_start at each incident (shared 5h projection is insufficient)"
        )
    data = read_json(destination) or {}
    data.update(
        codex_weekly_token_cap=statistics.median(r["weekly_tokens"] for r in evidence),
        codex_period_start=max(evidence, key=lambda r: r["ts"])["period_start"],
        evidence=evidence,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.replace(destination)
    return True, json.dumps(data, indent=2)


def calibration_misses(
    incidents: list[dict], snapshots: list[dict], *, handoff: Path, calibration_path: Path
) -> list[str]:
    """Correct an optimistic cap only from a fresh pre-incident model observation."""
    calibration = read_json(calibration_path) or {}
    seen = {r.get("incident_id") for r in read_rows(handoff / "pacing-calibration-misses.ndjson")}
    messages = []
    for incident in incidents:
        key = incident.get("incident_id")
        ts = pacing.timestamp(incident.get("ts"))
        pool = incident.get("agent")
        if (
            not key
            or key in seen
            or ts is None
            or pool not in ("codex", "claude")
            or incident.get("category") not in ("quota", "rate_limit", "capacity")
        ):
            continue
        matches = [s for s in snapshots if 0 <= ts - s.get("ts", 0) <= pacing.STALE_AFTER_SECONDS]
        if not matches:
            continue
        snapshot = max(matches, key=lambda s: s["ts"])
        row = (snapshot.get("agents", {}).get(pool) or {}).get("pacing") or {}
        if row.get("state") != "ok" or row.get("stale"):
            continue
        old = calibration.get(f"{pool}_weekly_token_cap")
        used = row.get("used_fraction")
        new = pacing.calibration_miss(old, used) if old is not None and used is not None else None
        miss = dict(
            ts=ts,
            incident_id=key,
            agent=pool,
            event="calibration_miss",
            old_cap=old,
            new_cap=new,
            used_fraction=used,
            source=row.get("source"),
        )
        if new is not None and old is not None and 0 < new < old:
            calibration[f"{pool}_weekly_token_cap"] = new
            calibration.setdefault("calibration_misses", []).append(miss)
            temp = calibration_path.with_suffix(".tmp")
            temp.write_text(json.dumps(calibration, indent=2) + "\n")
            temp.replace(calibration_path)
        append_rows(handoff / "pacing-calibration-misses.ndjson", [miss])
        seen.add(key)
        messages.append(
            f"FYI {pool}: calibration miss at {ts:.0f}; cap={old} → {new}; "
            "unmeasured correction needs a calibrated cap and observed spend"
            if new is None
            else f"FYI {pool}: calibration miss at {ts:.0f}; cap={old} → {new}"
        )
    return messages


def integrate(agents: dict, *, now: float, handoff: Path, record: bool = False) -> list[str]:
    inventory, messages = reset_inventory(RESETS_FILE, now)
    calibration = read_json(CALIBRATION_FILE) or {}
    samples_path = handoff / "pacing-usage-samples.ndjson"
    samples = read_rows(samples_path)
    incidents = read_rows(handoff / "rate-limit-incidents.ndjson")
    if record:
        messages.extend(
            calibration_misses(
                incidents,
                [
                    r
                    for r in read_rows(handoff / "capacity-ledger.ndjson")
                    if r.get("event") == "pacing_snapshot"
                ],
                handoff=handoff,
                calibration_path=CALIBRATION_FILE,
            )
        )
        calibration = read_json(CALIBRATION_FILE) or {}
    previous = (read_json(handoff / "capacity.json") or {}).get("agents", {})
    new_samples = []
    for pool in ("codex", "claude"):
        entries = inventory.get(pool)
        reset_times = [
            t
            for e in entries or []
            if (t := pacing.timestamp(e.get("consumed_at"))) is not None and t <= now
        ]
        reset_at = max(reset_times) if reset_times else None
        bounds = pacing.period(
            pool, now, start=calibration.get("codex_period_start"), reset_at=reset_at
        )
        source_calibration = dict(calibration)
        if pool == "codex" and bounds:
            source_calibration["codex_period_start"] = bounds[0]
        datum = usage(pool, now, source_calibration, snapshots=SNAPSHOTS, claude_path=CLAUDE_USAGE)
        if pool == "codex" and datum.get("period_end") is not None:
            messages.extend(consume_detected_resets(inventory, datum, now=now, record=record))
            bounds = (datum["period_end"] - pacing.PERIOD_SECONDS, datum["period_end"])
        start, end = bounds if bounds else (None, None)
        if bounds and datum.get("ts") is not None and datum["ts"] < bounds[0]:
            datum["used_fraction"] = None
        exhausted: list[float] = []
        if bounds:
            start, end = bounds
            exhausted = [
                t
                for r in incidents
                if r.get("agent") == pool
                and r.get("category") in ("quota", "rate_limit", "capacity")
                and (t := pacing.timestamp(r.get("ts"))) is not None
                and start <= t <= now
                and (reset_at is None or t > reset_at)
            ]
            exhausted += [
                r["ts"]
                for r in samples
                if r.get("agent") == pool
                and r.get("ts") is not None
                and start <= r["ts"] <= now
                and r.get("used_fraction") is not None
                and r["used_fraction"] >= 1
                and (reset_at is None or r["ts"] > reset_at)
            ]
        sample = dict(agent=pool, recorded_at=now, **datum)
        sample.setdefault("period_start", start)
        pool_samples = [r for r in samples if r.get("agent") == pool and r.get("ts") is not None]
        if datum["ts"] is not None and now - datum["ts"] <= pacing.STALE_AFTER_SECONDS:
            # Fresh percent observations sampled on ticks measure flat spend too;
            # source_ts retains the app observation time so freshness cannot be forged.
            sample.update(source_ts=datum["ts"], ts=now)
            pool_samples.append(sample)
        new_samples.append(sample)
        prior = (previous.get(pool) or {}).get("pacing") or {}
        row = pacing.evaluate(
            pool,
            now=now,
            period_start=start,
            period_end=end,
            resets=entries,
            samples=pool_samples,
            exhausted_at=max(exhausted) if exhausted else None,
            previous_state=prior.get("state"),
            source_ts=datum["ts"],
            used_fraction=datum["used_fraction"],
            source=datum["source"],
        )
        # Infrastructure/auth/explicit shed markers remain authoritative.
        agent = agents[pool]
        ranks = {"ok": 0, "warn": 1, "shed": 2}
        if row["state"] in ranks and agent["state"] in ranks:
            agent["state"] = max((agent["state"], row["state"]), key=lambda state: ranks[state])
        agent["pacing"] = row
        agent["reason"] += f"; pacing.state={row['state']}; " + row["reason"]
        messages.extend(row["fyi"])
    if record:
        append_rows(samples_path, new_samples)
        append_rows(
            handoff / "capacity-ledger.ndjson",
            [
                dict(
                    ts=now,
                    event="pacing_snapshot",
                    agents={
                        k: dict(state=v["state"], pacing=v.get("pacing")) for k, v in agents.items()
                    },
                )
            ],
        )
    return messages


def report(handoff: Path, *, now: float, current: dict | None = None) -> str:
    snapshots = [
        r
        for r in read_rows(handoff / "capacity-ledger.ndjson")
        if r.get("event") == "pacing_snapshot" and r.get("ts", now + 1) <= now
    ]
    snapshots.sort(key=lambda r: r["ts"])
    current = current if current is not None else (read_json(handoff / "capacity.json") or {})
    lines = [
        "Weekly pacing check: python3 src/capacity.py pacing-report",
        "Evidence class: usage store + rate-limit ledger (dispatch share: capacity-ledger dispatch starts)",
    ]
    lines.extend(current.get("pacing_fyi", []))

    def measured(value):
        return "unmeasured" if value is None else value

    for pool in ("codex", "claude"):
        row = (current.get("agents", {}).get(pool) or {}).get("pacing") or {}
        lines.append(
            f"pool {pool}: u={measured(row.get('used_fraction'))} "
            f"ũ={measured(row.get('projected_fraction'))} "
            f"state={row.get('state', 'unknown')} w_p={measured(row.get('routing_weight'))} "
            f"resets={row.get('resets_available') if row.get('resets_available') is not None else 'unknown'} source={row.get('source', 'none')} "
            f"ts={row.get('ts')} stale={row.get('ts') is not None and now-row['ts'] > pacing.STALE_AFTER_SECONDS}; "
            f"{row.get('reason', 'usage unmeasured; clears on fresh usage/period sample')}"
        )
        finals = []
        for snap in snapshots:
            p = (snap.get("agents", {}).get(pool) or {}).get("pacing") or {}
            end = p.get("period_end")
            if (
                end is not None
                and end <= now
                and 0 <= end - snap["ts"] <= pacing.STALE_AFTER_SECONDS
            ):
                if p.get("used_fraction") is not None and not p.get("stale"):
                    finals.append(p)
        if finals:
            final = max(finals, key=lambda r: r["period_end"])
            lo, hi = pacing.pass_bounds(final["effective_reserve"])
            u = final["used_fraction"]
            lines.append(
                f"1 {pool}: u(T1)={u:.3f} bounds=[{lo:.3f},{hi:.3f}] "
                f"{'PASS' if lo <= u <= hi else 'FAIL'} T1={final['period_end']} [usage store + rate-limit ledger]"
            )
        else:
            lines.append(
                f"1 {pool}: unmeasured — would need a fresh usage sample at completed T1 "
                "[usage store + rate-limit ledger]"
            )
    # Coverage gaps prevent a missing incident/dispatch baseline from becoming a measured zero.
    incidents = read_rows(handoff / "rate-limit-incidents.ndjson")
    events = read_rows(handoff / "capacity-ledger.ndjson")
    starts = [
        r for r in events if r.get("event") == "start" and r.get("agent") and r.get("count", 0) > 0
    ]

    def observed(ts):
        matches = [s for s in snapshots if 0 <= ts - s["ts"] <= pacing.STALE_AFTER_SECONDS]
        return matches[-1]["agents"] if matches else None

    # Use the latest completed pool period for both population queries.
    periods: list[tuple[float, float]] = []
    for snapshot in snapshots:
        for pool in ("codex", "claude"):
            p = (snapshot.get("agents", {}).get(pool) or {}).get("pacing") or {}
            begin, finish = p.get("period_start"), p.get("period_end")
            if begin is not None and finish is not None and finish <= now:
                periods.append((float(begin), float(finish)))
    if not periods:
        lines += [
            f"2: unmeasured — would need completed-period snapshots and rate-limit ledger coverage; coverage=0/unknown ticks; observed incidents={len(incidents)} [usage store + rate-limit ledger]",
            f"3: unmeasured — would need dispatches in both codex ok and warn/shed hours; coverage=0/unknown ticks; observed dispatches={len(starts)} [usage store + rate-limit ledger]",
        ]
        return "\n".join(lines)
    start, end = max(periods, key=lambda p: p[1])
    relevant = [r for r in incidents if start <= (pacing.timestamp(r.get("ts")) or 0) < end]
    ticks = list(range(int(start), int(end), pacing.TICK_SECONDS))
    # Each 15-minute slot needs its own observation; a neighbouring tick cannot fill a gap.
    covered = sum(any(t <= s["ts"] < t + pacing.TICK_SECONDS for s in snapshots) for t in ticks)
    coverage = covered == len(ticks)
    coverage_text = f"coverage={covered}/{len(ticks)} ticks"
    classified = [r for r in relevant if observed(pacing.timestamp(r["ts"])) is not None]
    violations = sum(
        any(
            a != r.get("agent") and v.get("state") == "ok"
            for a, v in observed(pacing.timestamp(r["ts"])).items()
        )
        for r in classified
    )
    if (
        coverage
        and len(classified) == len(relevant)
        and (handoff / "rate-limit-incidents.ndjson").exists()
    ):
        lines.append(
            f"2: {violations} incidents while another pool ok; {coverage_text}; {'PASS' if violations == 0 else 'FAIL'} [usage store + rate-limit ledger]"
        )
    else:
        lines.append(
            f"2: unmeasured — would need full-period ledger/snapshot coverage; {coverage_text}; observed violations={violations}; incidents={len(classified)}/{len(relevant)} [usage store + rate-limit ledger]"
        )
    groups = {"ok": [0, 0], "constrained": [0, 0]}
    omitted = 0
    for event in starts:
        ts = pacing.timestamp(event.get("ts"))
        if ts is None or not start <= ts < end:
            continue
        state = ((observed(ts) or {}).get("codex") or {}).get("state")
        group = "ok" if state == "ok" else "constrained" if state in ("warn", "shed") else None
        if group is None:
            omitted += 1
            continue
        groups[group][0] += int(event["agent"] != "codex")
        groups[group][1] += 1
    if coverage and all(g[1] > 0 for g in groups.values()) and omitted == 0:
        base = groups["ok"][0] / groups["ok"][1]
        constrained = groups["constrained"][0] / groups["constrained"][1]
        passed = constrained > 0 and constrained >= pacing.PASS_SHARE_MULTIPLIER * base
        verdict = (
            ("PASS" if passed else "FAIL")
            if constrained > 0
            else "unmeasured — no non-codex dispatches"
        )
        lines.append(
            f"3: non-codex share ok={base:.3f}, warn/shed={constrained:.3f}; "
            f"target ≥{pacing.PASS_SHARE_MULTIPLIER}×; {verdict}; {coverage_text}; "
            f"counts={groups} [usage store + rate-limit ledger]"
        )
    else:
        lines.append(
            f"3: unmeasured — would need dispatches in both codex ok and warn/shed hours; "
            f"{coverage_text}; counts={groups}, unclassified={omitted} [usage store + rate-limit ledger]"
        )
    return "\n".join(lines)
