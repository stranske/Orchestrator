"""Local pacing evidence I/O. No credentials, browser scraping or provider calls.

Codex: freshest scheduled app-server poll or session rollout weekly primary.
Claude: freshest scheduled /usage all-models week or desktop percentage history.
Polls: ~/.codex/orchestrator/usage-poll/{codex,claude}-usage.json; original ts
controls freshness. Weekly poll reset epochs define T1, T0=T1-7d. Fresh Codex
credit statuses/expiries are authoritative; no identifiers enter pacing evidence.
Fallback: agent-specific weekly-*.json plus timestamp-matched quota calibration.
Mixed-agent ccusage totals are never silently attributed to Codex or Claude.
"""

from __future__ import annotations

import hashlib
import json
import os
import statistics
import sys
import tempfile
import time
from bisect import bisect_right
from pathlib import Path

import pacing

STATE_DIR = Path(os.environ.get("ORCH_STATE_DIR", Path.home() / ".codex/orchestrator"))
RESETS_FILE = STATE_DIR / "capacity-resets.json"
CALIBRATION_FILE = STATE_DIR / "capacity-calibration.json"
CLAUDE_USAGE = Path.home() / "Library/Application Support/Claude/plan-usage-history.json"
SNAPSHOTS = Path.home() / ".codex/usage-snapshots"
CODEX_SESSIONS = Path.home() / ".codex/sessions"
ROLLOUT_FILE_LIMIT = 300
POLL_DIR = STATE_DIR / "usage-poll"
POLL_HISTORY_TAIL_BYTES = 2 * 1024 * 1024


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
    except OSError:
        return []
    if bad:
        print(f"FYI {path.name}: {bad} malformed rows omitted", file=sys.stderr)
    return rows


def read_poll_tail(path: Path) -> list[dict]:
    """Bound hot-path reads independently of poller installation/rotation age."""
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            offset = max(0, size - POLL_HISTORY_TAIL_BYTES)
            stream.seek(offset)
            if offset:
                stream.readline(POLL_HISTORY_TAIL_BYTES)  # Discard a possibly partial record.
            payload = stream.read(POLL_HISTORY_TAIL_BYTES)
    except OSError:
        return []
    if offset:
        print(
            f"FYI {path.name}: tail limited to {POLL_HISTORY_TAIL_BYTES} bytes; "
            f"older_bytes_omitted={size - len(payload)}",
            file=sys.stderr,
        )
    rows, bad = [], 0
    for line in payload.splitlines():
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError
            rows.append(row)
        except ValueError:
            bad += 1
    if bad:
        print(f"FYI {path.name}: {bad} malformed tail rows omitted", file=sys.stderr)
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
        for entry in entries:
            if entry.get("consumed_at"):
                messages.append(f"FYI {pool}: reset consumed at {entry['consumed_at']}")
            elif (expiry := pacing.timestamp(entry["expires"])) is not None and expiry <= now:
                messages.append(f"FYI {pool}: reset expired {entry['expires']}")
            else:
                messages.append(
                    f"FYI {pool}: reset expires {entry['expires']}; "
                    "hand-file estimate; fresh provider expiries take precedence"
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


def rollout_observations(root: Path, now: float, *, stats: dict | None = None) -> list[dict]:
    """Cache sanitized observations and append offsets; never cache raw payloads.

    Rollouts are append-only. Inode, size, mtime and boundary fingerprints detect
    replacement/truncation before reusing an offset. Incomplete final lines are
    retried. Cache failure affects speed only, never source freshness or admission.
    """
    started = time.monotonic()
    root_key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()
    cache_path = Path(tempfile.gettempdir()) / f"orch-pacing-{os.getuid()}-{root_key}.json"
    cache = read_json(cache_path)
    cache = cache if isinstance(cache, dict) else {}
    cached = cache.get("files", {}) if cache.get("version") == 1 else {}
    unreadable = vanished = total_files = 0
    candidates = []
    try:
        for path in root.glob("**/rollout-*.jsonl"):
            total_files += 1
            try:
                candidates.append((path.stat().st_mtime, path))
            except FileNotFoundError:
                vanished += 1
                unreadable += 1
            except OSError:
                unreadable += 1
    except OSError:
        unreadable += 1
    files = [path for _, path in sorted(candidates, key=lambda pair: pair[0], reverse=True)]
    omitted = max(0, len(files) - ROLLOUT_FILE_LIMIT)
    rows: list[dict] = []
    updated = {}
    malformed = files_read = files_cached = bytes_read = 0
    for path in files[:ROLLOUT_FILE_LIMIT]:
        try:
            stat = path.stat()
            prior = cached.get(str(path), {})
            with path.open("rb") as stream:
                offset = prior.get("offset", 0)
                prefix_size = min(offset, 4096)
                prefix = hashlib.sha256(stream.read(prefix_size)).hexdigest()
                stream.seek(max(0, offset - 4096))
                boundary = hashlib.sha256(stream.read(min(offset, 4096))).hexdigest()
                reusable = (
                    prior.get("inode") == stat.st_ino
                    and prior.get("device") == stat.st_dev
                    and stat.st_size >= prior.get("size", 0) >= offset
                    and prior.get("prefix") == prefix
                    and prior.get("boundary") == boundary
                    and (
                        stat.st_size > prior.get("size", 0)
                        or stat.st_mtime_ns == prior.get("mtime")
                    )
                )
                file_rows = prior.get("rows", []) if reusable else []
                file_bad = prior.get("malformed", 0) if reusable else 0
                offset = offset if reusable else 0
                stream.seek(offset)
                if reusable and offset == stat.st_size:
                    files_cached += 1
                else:
                    files_read += 1
                while line := stream.readline():
                    bytes_read += len(line)
                    if not line.endswith(b"\n"):
                        break
                    offset = stream.tell()
                    if b'"token_count"' not in line or b'"rate_limits"' not in line:
                        continue
                    try:
                        event = json.loads(line)
                        payload = event.get("payload") or {}
                        limits = payload.get("rate_limits") or {}
                        primary = limits.get("primary") or {}
                        secondary = limits.get("secondary") or {}
                        ts = pacing.timestamp(event.get("timestamp"))
                        percent = primary.get("used_percent")
                        end = pacing.timestamp(primary.get("resets_at"))
                        if (
                            event.get("type") == "event_msg"
                            and payload.get("type") == "token_count"
                            and primary.get("window_minutes") == 10080
                            and ts is not None
                            and end is not None
                            and isinstance(percent, (int, float))
                            and 0 <= percent <= 100
                        ):
                            short = secondary.get("used_percent")
                            file_rows.append(
                                dict(
                                    ts=ts,
                                    used_fraction=percent / 100,
                                    period_end=end,
                                    short_used_fraction=(
                                        short / 100
                                        if secondary.get("window_minutes") == 300
                                        and isinstance(short, (int, float))
                                        and 0 <= short <= 100
                                        else None
                                    ),
                                    source="codex-rollout:rate_limits.primary",
                                )
                            )
                    except (ValueError, TypeError, AttributeError):
                        file_bad += 1
                stream.seek(0)
                prefix = hashlib.sha256(stream.read(min(offset, 4096))).hexdigest()
                stream.seek(max(0, offset - 4096))
                boundary = hashlib.sha256(stream.read(min(offset, 4096))).hexdigest()
                updated[str(path)] = dict(
                    inode=stat.st_ino,
                    device=stat.st_dev,
                    size=stat.st_size,
                    mtime=stat.st_mtime_ns,
                    offset=offset,
                    prefix=prefix,
                    boundary=boundary,
                    rows=file_rows,
                    malformed=file_bad,
                )
                rows.extend(r for r in file_rows if r["ts"] <= now)
                malformed += file_bad
        except FileNotFoundError:
            vanished += 1
            unreadable += 1
        except OSError:
            unreadable += 1
    try:
        # Private temporary + atomic replace: concurrent readers see a complete cache.
        with tempfile.NamedTemporaryFile(mode="w", dir=cache_path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(dict(version=1, files=updated), stream, allow_nan=False)
        temporary.replace(cache_path)
    except OSError:
        print(
            "FYI codex rollout cache unavailable; source scan remains authoritative",
            file=sys.stderr,
        )
    scan = dict(
        duration_seconds=round(time.monotonic() - started, 4),
        files_read=files_read,
        files_cached=files_cached,
        bytes_read=bytes_read,
        total_files=total_files,
        omitted=omitted,
        malformed=malformed,
        unreadable_files=unreadable,
        vanished_files=vanished,
    )
    if stats is not None:
        stats.update(scan)
    print(
        "FYI codex rollout scan: " + ", ".join(f"{k}={v}" for k, v in scan.items()), file=sys.stderr
    )
    return sorted(rows, key=lambda r: r["ts"])


def codex_rollout_usage(root: Path, now: float) -> dict | None:
    stats: dict = {}
    rows = rollout_observations(root, now, stats=stats)
    if not rows:
        return None
    return dict(rows[-1], **reset_changes(rows), scan=stats)


def reset_changes(rows: list[dict]) -> dict:
    rows = sorted(rows, key=lambda r: r["ts"])
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
    return dict(reset_events=sorted(set(resets)), reset_signals=sorted(set(signals)))


def consume_detected_resets(inventory: dict, datum: dict, *, now: float, record: bool) -> list[str]:
    """Idempotently debit oldest-expiring bank entries, preserving the audit trail."""
    poll = datum.get("poll") or {}
    if poll.get("fresh"):
        # A plan rollover is not proof of a credit consumption. Never mutate the
        # hand inventory while the provider supplies the actual inventory.
        return [
            "FYI codex: fresh poll inventory; auto-debit disabled; "
            f"reset signals={len(datum.get('reset_signals', []))}; "
            f"available={poll.get('resets_available', 'unknown')}"
        ]
    entries = inventory.get("codex")
    if not isinstance(entries, list):
        return []
    messages = [
        f"FYI codex: reset signals={len(datum.get('reset_signals', []))}; "
        f"confirmed bank consumptions={len(datum.get('reset_events', []))}; "
        "auto-debit requires usage drop + >one-tick advance of T1 before scheduled rollover"
    ]
    baseline = pacing.timestamp(inventory.get("observed_at")) or 0
    if poll.get("resets") is not None and poll.get("ts") is not None:
        baseline = max(baseline, poll["ts"])
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


def fraction(value):
    return value / 100 if isinstance(value, (int, float)) and 0 <= value <= 100 else None


def polled_usage(pool: str, now: float, directory: Path | None = None, *, data=None) -> dict:
    """Allowlist poll fields once for selection, marker recovery, health and reports."""
    directory = POLL_DIR if directory is None else directory
    data = read_json(directory / f"{pool}-usage.json") if data is None else data
    data = data if isinstance(data, dict) else {}
    ts = pacing.timestamp(data.get("ts"))
    if ts is not None and ts > now:
        ts = None
    window = data.get("primary" if pool == "codex" else "weekly") or {}
    if not isinstance(window, dict):
        window = {}
    used = fraction(window.get("used_percent"))
    end = pacing.timestamp(window.get("resets_at"))
    valid = used is not None and end is not None
    if pool == "codex":
        valid = valid and window.get("window_minutes") == 10080
    else:
        valid = valid and str(window.get("label", "")).lower() == "all models"
    short = data.get("secondary" if pool == "codex" else "session") or {}
    if not isinstance(short, dict):
        short = {}
    age = now - ts if ts is not None else None
    fresh = valid and age is not None and age <= pacing.STALE_AFTER_SECONDS
    poll: dict = dict(
        ts=ts,
        age=age,
        fresh=fresh,
        valid=valid,
        ticks_missed=max(0, int(age // pacing.POLL_TICK_SECONDS) - 1) if age is not None else None,
    )
    if pool == "claude" and valid and end is not None and end <= now:
        old_end = end
        end += (int((now - end) // pacing.PERIOD_SECONDS) + 1) * pacing.PERIOD_SECONDS
        poll.update(observed_period_end=old_end, rolled_period_end=end)
        used = None  # An expired weekly window always describes the old period.
        poll["fresh"] = False
    if pool == "codex":
        credits = data.get("reset_credits")
        entries = None
        if isinstance(credits, list) and all(
            isinstance(c, dict)
            and pacing.timestamp(c.get("expires_at")) is not None
            and isinstance(c.get("status"), str)
            for c in credits
        ):
            entries = [
                dict(expires=c["expires_at"], source="codex-app-server")
                for c in credits
                if c["status"] == "available" and (pacing.timestamp(c["expires_at"]) or 0) > now
            ]
        poll.update(resets=entries, resets_available=len(entries) if entries is not None else None)
    else:
        poll["all_models_used_fraction"] = used
        poll["weekly_buckets"] = [
            dict(
                label=b["label"],
                used_fraction=fraction(b.get("used_percent")),
                period_end=pacing.timestamp(b.get("resets_at")),
            )
            for b in (data.get("buckets") or [])
            if isinstance(b, dict)
            and b.get("kind") == "week"
            and isinstance(b.get("label"), str)
            and b["label"].lower() != "all models"
        ]
    return dict(
        ts=ts,
        used_fraction=used if valid else None,
        period_end=end,
        short_used_fraction=(
            fraction(short.get("used_percent"))
            if pool == "claude" or short.get("window_minutes") == 300
            else None
        ),
        source=(
            "codex-app-server:account/rateLimits/read" if pool == "codex" else "claude-cli:/usage"
        ),
        poll=poll,
    )


def poller_health(pool: str, poll: dict) -> list[str]:
    age, ticks = poll.get("age"), poll.get("ticks_missed")
    age = round(age, 1) if age is not None else "unknown"
    ticks = round(ticks, 2) if ticks is not None else "unknown"
    allowance = f"allowance={pacing.POLL_STALE_TICKS} ticks/{pacing.STALE_AFTER_SECONDS}s"
    lines = [
        f"POLLER {pool}: last ok ts={poll.get('ts')}; age={age}; "
        f"ticks missed={ticks}; {allowance}; valid={poll.get('valid')}"
    ]
    if poll.get("rolled_period_end") is not None:
        lines.append(
            f"FYI {pool}: old T1={poll['observed_period_end']}; "
            f"rolled T1={poll['rolled_period_end']}; poll age={age}s; "
            "new-period usage requires a new-period observation"
        )
    if not poll.get("fresh"):
        lines.append(
            f"POLLER STALE: {pool} age={age}s — launchd com.stranske.usage-poll; "
            f"{allowance}; drain: successful scheduled poll"
        )
    return lines


def poll_coverage(
    start: float,
    end: float,
    *,
    now: float,
    snapshots: list[dict] | None = None,
    stats: dict | None = None,
) -> tuple[int, int]:
    """A slot uses each provider's latest valid poll at/before its timestamp.

    The one stale allowance applies to that poll, including a pre-period poll.
    A gap over the allowance leaves at least one slot uncovered even between ticks.
    Snapshot freshness remains an independent requirement for passes 2 and 3.
    """
    ticks = range(int(start), int(end), pacing.POLL_TICK_SECONDS)
    present = {}
    gaps = []
    for pool in ("codex", "claude"):
        rows = read_rows(POLL_DIR / f"{pool}-usage.ndjson")
        latest = read_json(POLL_DIR / f"{pool}-usage.json")
        if isinstance(latest, dict):
            rows.append(latest)
        timestamps = sorted(
            {
                datum["ts"]
                for row in rows
                if (datum := polled_usage(pool, now, data=row))["poll"]["valid"]
                and datum["ts"] is not None
                and datum["ts"] <= end
            }
        )
        slots = set()
        for slot, tick in enumerate(ticks):
            index = bisect_right(timestamps, tick) - 1
            if index >= 0 and tick - timestamps[index] <= pacing.STALE_AFTER_SECONDS:
                slots.add(slot)
        present[pool] = slots
        prior = bisect_right(timestamps, start) - 1
        points = (
            ([timestamps[prior]] if prior >= 0 else [start])
            + [ts for ts in timestamps if start < ts < end]
            + [end]
        )
        gaps.append(max((b - a for a, b in zip(points, points[1:])), default=0))
    covered = present["codex"] & present["claude"]
    if snapshots is not None:
        covered = {
            slot
            for slot in covered
            if all(
                (observed_agents(snapshots, int(start) + slot * pacing.POLL_TICK_SECONDS) or {})
                .get(pool, {})
                .get("state")
                in ("ok", "warn", "shed")
                for pool in ("codex", "claude")
            )
        }
    longest_gap = max(gaps, default=0)
    covered_count = len(covered)
    if covered_count == len(ticks) and covered_count and longest_gap > pacing.STALE_AFTER_SECONDS:
        # Sampling can miss a stale interval that starts and ends between ticks.
        covered_count -= 1
    if stats is not None:
        stats["longest_gap"] = longest_gap
    return covered_count, len(ticks)


def observed_agents(snapshots: list[dict], ts: float) -> dict | None:
    matches = [s for s in snapshots if 0 <= ts - s["ts"] <= pacing.STALE_AFTER_SECONDS]
    return max(matches, key=lambda s: s["ts"])["agents"] if matches else None


def usage(
    pool: str,
    now: float,
    calibration: dict,
    snapshots: Path = SNAPSHOTS,
    claude_path: Path = CLAUDE_USAGE,
    sessions: Path | None = None,
    poll_dir: Path | None = None,
) -> dict:
    polled = polled_usage(pool, now, poll_dir)
    candidates = []
    if polled["ts"] is not None and (
        polled["used_fraction"] is not None or polled["poll"].get("rolled_period_end") is not None
    ):
        candidates.append(polled)
    if pool == "codex":
        observed = codex_rollout_usage(CODEX_SESSIONS if sessions is None else sessions, now)
        if observed is not None:
            candidates.append(observed)
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
                candidates.append(
                    dict(
                        used_fraction=(
                            row["u"]["sd"] / 100
                            if not polled["poll"].get("rolled_period_end")
                            or ts >= polled["period_end"] - pacing.PERIOD_SECONDS
                            else None
                        ),
                        period_end=polled.get("period_end"),
                        ts=ts,
                        short_used_fraction=(
                            row["u"]["fh"] / 100
                            if isinstance(row["u"].get("fh"), (int, float))
                            and 0 <= row["u"]["fh"] <= 100
                            else None
                        ),
                        source="claude-desktop:plan-usage-history.json:u.sd",
                    )
                )
    if candidates:
        selected = dict(max(candidates, key=lambda r: r["ts"]))
        selected["poll"] = polled["poll"]
        # Short-window recovery must use its own timestamp when the freshest
        # weekly datum is a rollout/desktop observation.
        if polled["poll"]["fresh"] and polled["short_used_fraction"] is not None:
            selected.update(
                short_used_fraction=polled["short_used_fraction"], short_ts=polled["ts"]
            )
        else:
            selected["short_ts"] = selected["ts"]
        if pool == "codex":
            rollout = next((r for r in candidates if r.get("scan")), {})
            directory = POLL_DIR if poll_dir is None else poll_dir
            historical = [
                polled_usage(pool, now, directory, data=r)
                for r in read_poll_tail(directory / "codex-usage.ndjson")
            ]
            observations = [
                r
                for r in historical + candidates
                if r.get("ts") is not None
                and r.get("used_fraction") is not None
                and r.get("period_end") is not None
            ]
            changes = reset_changes(observations)
            for key in ("reset_events", "reset_signals"):
                selected[key] = sorted(set(changes[key] + rollout.get(key, [])))
            if rollout.get("scan"):
                selected["scan"] = rollout["scan"]
        return selected
    cap = calibration.get(f"{pool}_weekly_token_cap")
    paths = []
    unreadable = 0
    try:
        for path in snapshots.glob("weekly-*.json"):
            try:
                paths.append((path.stat().st_mtime, path))
            except OSError:
                unreadable += 1
    except OSError:
        unreadable += 1
    if unreadable:
        print(f"FYI weekly snapshot scan: unreadable_files={unreadable}", file=sys.stderr)
    for mtime, path in sorted(paths, key=lambda pair: pair[0], reverse=True):
        rows = weekly_tokens(path, pool)
        if not rows:
            continue
        row = max(rows, key=lambda r: r["period_start"])
        if not isinstance(cap, (int, float)) or cap <= 0:
            return dict(
                used_fraction=None,
                ts=mtime,
                source="ccusage-unscaled:weekly-agent-tokens",
                poll=polled["poll"],
                weekly_tokens=row["tokens"],
                period_start=row["period_start"],
            )
        # Calendar-week aggregates cannot measure a bank-reset-shifted week.
        bounds = pacing.period(
            pool,
            now,
            start=calibration.get("codex_period_start"),
            end=calibration.get("claude_period_end"),
        )
        if bounds is None or abs(row["period_start"] - bounds[0]) > 1:
            continue
        return dict(
            used_fraction=row["tokens"] / cap,
            ts=mtime,
            source="ccusage-calibrated:weekly-agent-tokens",
            poll=polled["poll"],
            weekly_tokens=row["tokens"],
            period_start=row["period_start"],
        )
    return dict(used_fraction=None, ts=None, source="none", poll=polled["poll"])


def usage_snapshot(now: float) -> dict:
    """One pool reading per build, preserving reset-aligned fallback periods."""
    inventory, _ = reset_inventory(RESETS_FILE, now)
    calibration = read_json(CALIBRATION_FILE) or {}
    result = {}
    for pool in ("codex", "claude"):
        times = [
            t
            for entry in inventory.get(pool, [])
            if (t := pacing.timestamp(entry.get("consumed_at"))) is not None and t <= now
        ]
        bounds = pacing.period(
            pool,
            now,
            start=calibration.get("codex_period_start"),
            end=calibration.get("claude_period_end"),
            reset_at=max(times) if times else None,
        )
        aligned = dict(calibration)
        if pool == "codex" and bounds:
            aligned["codex_period_start"] = bounds[0]
        result[pool] = usage(pool, now, aligned, snapshots=SNAPSHOTS, claude_path=CLAUDE_USAGE)
    return result


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


def integrate(
    agents: dict,
    *,
    now: float,
    handoff: Path,
    record: bool = False,
    usage_data: dict | None = None,
    previous: dict | None = None,
) -> list[str]:
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
    previous = previous if previous is not None else (read_json(handoff / "capacity.json") or {})
    previous_agents = previous.get("agents", {})
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
            pool,
            now,
            start=calibration.get("codex_period_start"),
            reset_at=reset_at,
            end=calibration.get("claude_period_end"),
        )
        source_calibration = dict(calibration)
        if pool == "codex" and bounds:
            source_calibration["codex_period_start"] = bounds[0]
        datum = (
            dict(usage_data[pool])
            if usage_data is not None
            else usage(pool, now, source_calibration, snapshots=SNAPSHOTS, claude_path=CLAUDE_USAGE)
        )
        poll = datum.get("poll") or polled_usage(pool, now)["poll"]
        datum["poll"] = poll
        messages.extend(poller_health(pool, poll))
        if pool == "claude":
            for bucket in poll.get("weekly_buckets", []):
                messages.append(
                    f"FYI claude weekly bucket {bucket['label']}: "
                    f"u={bucket['used_fraction']} T1={bucket['period_end']}"
                )
                if (
                    bucket["used_fraction"] is not None
                    and poll.get("all_models_used_fraction") is not None
                    and bucket["used_fraction"] > poll["all_models_used_fraction"]
                ):
                    messages.append(
                        f"FYI claude: {bucket['label']} weekly usage higher than "
                        "all models; informational (dispatch uses Sonnet)"
                    )
        elif poll.get("fresh"):
            hand_count = len(pacing.active_resets(entries, now)) if entries is not None else None
            entries = poll.get("resets")
            messages.append(
                f"FYI codex resets: poll={poll.get('resets_available')} "
                f"hand file={hand_count if hand_count is not None else 'unknown'}; "
                "using fresh poll; expiries=" + str([e["expires"] for e in entries or []])
            )
        # A recording tick can lower the calibrated cap before integration.
        if datum.get("source", "").startswith("ccusage-calibrated"):
            cap = source_calibration.get(f"{pool}_weekly_token_cap")
            if isinstance(cap, (int, float)) and cap > 0:
                datum["used_fraction"] = datum["weekly_tokens"] / cap
        if datum.get("scan"):
            messages.append(
                "FYI codex rollout scan: " + ", ".join(f"{k}={v}" for k, v in datum["scan"].items())
            )
        if pool == "codex" and datum.get("period_end") is not None:
            messages.extend(consume_detected_resets(inventory, datum, now=now, record=record))
        if datum.get("period_end") is not None:
            bounds = (datum["period_end"] - pacing.PERIOD_SECONDS, datum["period_end"])
        start, end = bounds if bounds else (None, None)
        if bounds and datum.get("ts") is not None and (datum["ts"] < bounds[0] or now >= bounds[1]):
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
        prior = (previous_agents.get(pool) or {}).get("pacing") or {}
        prior_diagnostics = (previous_agents.get(pool) or {}).get("pacing_diagnostics") or {}
        history = prior_diagnostics.get("weight_history") or {
            "period_start": prior.get("period_start"),
            "period_end": prior.get("period_end"),
            "reset_at": reset_at if (prior.get("ts") or 0) >= (reset_at or 0) else None,
        }
        # Missing measurements are not reset events. Retain both the last weight
        # and its boundaries through repeated stale/unmeasured rows; expire only
        # when an observed period or reset actually changes.
        same_history = (
            (start is None or history.get("period_start") == start)
            and (end is None or history.get("period_end") == end)
            and (reset_at is None or history.get("reset_at") == reset_at)
        )
        # Missing inventory cannot undo an observed reset. Preserve its boundary
        # for both burn clipping and weight carry without altering reset accounting.
        history_reset_at = reset_at if reset_at is not None else history.get("reset_at")
        if history_reset_at is not None:
            pool_samples = [r for r in pool_samples if r["ts"] >= history_reset_at]
        prior_weight = prior.get("routing_weight")
        if prior_weight is None:
            prior_weight = prior_diagnostics.get("previous_routing_weight")
        row = pacing.evaluate(
            pool,
            now=now,
            period_start=start,
            period_end=end,
            resets=entries,
            samples=pool_samples,
            exhausted_at=max(exhausted) if exhausted else None,
            previous_state=prior.get("state"),
            previous_weight=prior_weight if same_history else None,
            source_ts=datum["ts"],
            used_fraction=datum["used_fraction"],
            source=datum["source"],
        )
        # Infrastructure/auth/explicit shed markers remain authoritative.
        agent = agents[pool]
        ranks = {"ok": 0, "warn": 1, "shed": 2}
        if row["state"] in ranks and agent["state"] in ranks:
            agent["state"] = max((agent["state"], row["state"]), key=lambda state: ranks[state])
        # Report-only diagnostics must not become route-export semantic changes.
        # The exporter intentionally copies only the agent's stable pacing block.
        agent["pacing_diagnostics"] = {
            key: row.pop(key)
            for key in (
                "span_h",
                "confidence",
                "effective_projected_fraction",
                "previous_routing_weight",
                "capacity_budget",
                "n_usable",
                "sustainable_rate_per_hour",
                "burn_short_per_hour",
                "burn_period_per_hour",
                "burn_effective_per_hour",
                "target_routing_weight",
            )
        }
        agent["pacing_diagnostics"]["weight_history"] = {
            "period_start": start if start is not None else history.get("period_start"),
            "period_end": end if end is not None else history.get("period_end"),
            "reset_at": history_reset_at,
        }
        row["measurement"] = (
            "measured"
            if datum.get("used_fraction") is not None and not row.get("stale") and bounds
            else "unknown"
        )
        row["reset_expiries"] = (
            [e["expires"] for e in pacing.active_resets(entries, now)]
            if entries is not None
            else None
        )
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
                        k: dict(
                            state=v["state"],
                            pacing=v.get("pacing"),
                            pacing_diagnostics=v.get("pacing_diagnostics"),
                        )
                        for k, v in agents.items()
                    },
                )
            ],
        )
    return messages


def report(
    handoff: Path, *, now: float, current: dict | None = None, previous: dict | None = None
) -> str:
    snapshots = pacing_snapshots(handoff, now)
    previous = previous if previous is not None else (read_json(handoff / "capacity.json") or {})
    current = current if current is not None else previous
    lines = [
        "Weekly pacing check: python3 src/capacity.py pacing-report",
        "Evidence class: usage store + rate-limit ledger (dispatch share: capacity-ledger dispatch starts)",
    ]
    lines.extend(current.get("pacing_fyi", []))
    if not any(line.startswith("POLLER ") for line in lines):
        for pool in ("codex", "claude"):
            lines.extend(poller_health(pool, polled_usage(pool, now)["poll"]))

    def measured(value):
        return "unmeasured" if value is None else value

    for pool in ("codex", "claude"):
        agent = current.get("agents", {}).get(pool) or {}
        row = {**(agent.get("pacing") or {}), **(agent.get("pacing_diagnostics") or {})}
        routed_agent = previous.get("agents", {}).get(pool) or {}
        routed_row = routed_agent.get("pacing") or {}
        routed_weight = routed_row.get("routing_weight")
        report_weight = row.get("routing_weight")
        tick_ts = previous.get("generated_at")
        tick_age = "unmeasured" if tick_ts is None else f"{now - tick_ts:.0f}s"
        routed = "unmeasured" if routed_weight is None else f"{routed_weight:.3f}"
        computed = "unmeasured" if report_weight is None else f"{report_weight:.3f}"
        prior_weight = row.get("previous_routing_weight")
        persisted_prior = routed_weight
        if persisted_prior is None:
            persisted_prior = (routed_agent.get("pacing_diagnostics") or {}).get(
                "previous_routing_weight"
            )
        hypothetical = persisted_prior is None and prior_weight is not None
        prior = "unmeasured" if hypothetical or prior_weight is None else f"{prior_weight:.3f}"
        step_note = " (hypothetical step from an unpersisted default)" if hypothetical else ""
        difference = ""
        if routed_weight is not None and report_weight is not None:
            delta = report_weight - routed_weight
            # Ignore binary float noise at the strict 0.01 display boundary.
            if round(abs(delta), 12) > 0.01:
                difference = f" weight_delta={delta:+.3f}"
        shed_note = (
            "; shed: weight is informational; router uses the state tier "
            "(shed pools only when no ok/warn pool can take the task)"
            if "shed" in (agent.get("state"), row.get("state"), routed_agent.get("state"))
            else ""
        )
        lines.append(
            f"pool {pool}: u={measured(row.get('used_fraction'))} "
            f"ũ={measured(row.get('projected_fraction'))} "
            f"ũ_eff={measured(row.get('effective_projected_fraction'))} "
            f"span_h={measured(row.get('span_h'))} c={measured(row.get('confidence'))} "
            f"C={measured(row.get('capacity_budget'))} n_usable={measured(row.get('n_usable'))} "
            f"s={measured(row.get('sustainable_rate_per_hour'))} "
            f"ḃ_short={measured(row.get('burn_short_per_hour'))} "
            f"ḃ_period={measured(row.get('burn_period_per_hour'))} "
            f"ḃ_eff={measured(row.get('burn_effective_per_hour'))} "
            f"w_p_target={measured(row.get('target_routing_weight'))} "
            f"state={row.get('state', 'unknown')} "
            f"pacing={row.get('measurement', 'unknown')} "
            f"routed_weight={routed} (tick ts={measured(tick_ts)}, age={tick_age}) "
            f"report_weight={computed}{difference} "
            f"w_p_prev→w_p={prior}→{computed}{step_note} "
            f"resets={row.get('resets_available') if row.get('resets_available') is not None else 'unknown'} source={row.get('source', 'none')} "
            f"T1={row.get('period_end')} reset_expiries={row.get('reset_expiries')} "
            f"ts={row.get('ts')} stale={row.get('ts') is not None and now - row['ts'] > pacing.STALE_AFTER_SECONDS}; "
            f"{row.get('reason', 'usage unmeasured; clears on fresh usage/period sample')}{shed_note}"
        )
        final = final_usage(snapshots, pool, now)
        if final is not None:
            lines.append(
                f"1 {pool}: u(T1)={final['u']:.3f} bounds=[{final['lo']:.3f},{final['hi']:.3f}] "
                f"{'PASS' if final['passed'] else 'FAIL'} T1={final['period_end']} [usage store + rate-limit ledger]"
            )
        else:
            lines.append(
                f"1 {pool}: unmeasured — would need a fresh usage sample at completed T1 "
                "[usage store + rate-limit ledger]"
            )
    lines.extend(period_results(handoff, snapshots, now)["lines"])
    return "\n".join(lines)


def pacing_snapshots(handoff: Path, now: float) -> list[dict]:
    snapshots = [
        r
        for r in read_rows(handoff / "capacity-ledger.ndjson")
        if r.get("event") == "pacing_snapshot" and r.get("ts", now + 1) <= now
    ]
    snapshots.sort(key=lambda r: r["ts"])
    return snapshots


def final_usage(snapshots: list[dict], pool: str, now: float) -> dict | None:
    """Pass number 1 for one pool: u at the latest completed T1, or None when unmeasured."""
    finals = []
    for snap in snapshots:
        p = (snap.get("agents", {}).get(pool) or {}).get("pacing") or {}
        end = p.get("period_end")
        if end is not None and end <= now and 0 <= end - snap["ts"] <= pacing.STALE_AFTER_SECONDS:
            if p.get("used_fraction") is not None and not p.get("stale"):
                finals.append(p)
    if not finals:
        return None
    final = max(finals, key=lambda r: r["period_end"])
    lo, hi = pacing.pass_bounds(final["effective_reserve"])
    u = final["used_fraction"]
    return {"u": u, "lo": lo, "hi": hi, "passed": lo <= u <= hi, "period_end": final["period_end"]}


def period_results(handoff: Path, snapshots: list[dict], now: float) -> dict:
    """Pass numbers 2 and 3 over the latest completed pool period, with their report lines.

    `incidents`/`share` carry a measured value or None; None is unmeasured, never zero.
    """
    lines: list[str] = []
    out: dict = {"lines": lines, "incidents": None, "share": None, "period": None}
    # Coverage gaps prevent a missing incident/dispatch baseline from becoming a measured zero.
    incidents = read_rows(handoff / "rate-limit-incidents.ndjson")
    events = read_rows(handoff / "capacity-ledger.ndjson")
    starts = [
        r for r in events if r.get("event") == "start" and r.get("agent") and r.get("count", 0) > 0
    ]

    def observed(ts):
        return observed_agents(snapshots, ts)

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
            f"2: unmeasured — would need completed-period snapshots and rate-limit ledger coverage; coverage=0/unknown ticks; longest poll gap=unknown/1800s allowance; observed incidents={len(incidents)} [usage store + rate-limit ledger]",
            f"3: unmeasured — would need dispatches in both codex ok and warn/shed hours; coverage=0/unknown ticks; longest poll gap=unknown/1800s allowance; observed dispatches={len(starts)} [usage store + rate-limit ledger]",
        ]
        return out
    start, end = max(periods, key=lambda p: p[1])
    out["period"] = (start, end)
    relevant = [r for r in incidents if start <= (pacing.timestamp(r.get("ts")) or 0) < end]
    stats: dict = {}
    covered, expected = poll_coverage(start, end, now=now, snapshots=snapshots, stats=stats)
    coverage = covered == expected
    coverage_text = (
        f"coverage={covered}/{expected} ticks (poll ticks={pacing.POLL_TICK_SECONDS}s); "
        f"longest poll gap={stats['longest_gap']:.0f}s/"
        f"{pacing.STALE_AFTER_SECONDS}s allowance; uncovered={expected - covered} slots; "
        "drain: valid polls and fresh capacity snapshots throughout a completed period"
    )
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
        out["incidents"] = violations
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
        if constrained > 0 or base > 0:
            out["share"] = {"ok": base, "constrained": constrained, "passed": passed}
        verdict = (
            ("PASS" if passed else "FAIL")
            if constrained > 0 or base > 0
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
    return out


PASS_EVIDENCE = "usage store + rate-limit ledger"


def pass_numbers(handoff: Path, now: float) -> dict:
    """The three #668 pass numbers as data for the feature goals ledger; read-only.

    Each field is {value, evidence_class, as_of, reason}. A value of None is unmeasured and
    carries the report's own reason; it is never coerced to zero.
    """
    snapshots = pacing_snapshots(handoff, now)
    as_of = int(now)
    finals = {pool: final_usage(snapshots, pool, now) for pool in ("codex", "claude")}
    measured = {pool: f for pool, f in finals.items() if f is not None}
    detail = "; ".join(
        (
            f"{pool} u(T1)={f['u']:.3f} in [{f['lo']:.3f},{f['hi']:.3f}]={f['passed']}"
            if f is not None
            else f"{pool} unmeasured (no fresh usage sample at a completed T1)"
        )
        for pool, f in finals.items()
    )
    # Share of BOTH pools in band: an unmeasured pool has not shown it is in band, so one pool in
    # band and one unmeasured reads 0.5, never 1.0. None only when neither pool is measured.
    usage = {
        "value": (sum(f["passed"] for f in measured.values()) / len(finals) if measured else None),
        "evidence_class": PASS_EVIDENCE,
        "as_of": as_of,
        "reason": detail,
    }
    period = period_results(handoff, snapshots, now)
    line2, line3 = (period["lines"] + ["", ""])[:2]
    share = period["share"]
    return {
        "usage_in_target_band": usage,
        "quota_incidents_while_other_ok": {
            "value": period["incidents"],
            "evidence_class": PASS_EVIDENCE,
            "as_of": as_of,
            "reason": line2[:400],
        },
        "non_codex_share_at_least_double": {
            "value": None if share is None else float(share["passed"]),
            "evidence_class": PASS_EVIDENCE,
            "as_of": as_of,
            "reason": line3[:400],
        },
    }
