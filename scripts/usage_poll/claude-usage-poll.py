#!/usr/bin/env python3
"""Scheduled read-only Claude /usage poll. No model turn; never log raw CLI output.

Parsers are pure/offline-testable. A failed command or parse leaves latest intact.
"""

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

OUT = Path.home() / ".codex/orchestrator/usage-poll"
LINE = re.compile(
    r"^Current (session|week)(?: \(([^)]*)\))?: (\d+)% used\s*·\s*resets (.+?) \(([^)]+)\)\s*$"
)
WHEN = re.compile(r"^(?:(\w{3}) (\d{1,2}) at )?(\d{1,2})(?::(\d{2}))?\s*(am|pm)$", re.I)


def parse_reset(text, tz, now):
    match = WHEN.fullmatch(text.strip())
    if not match:
        raise ValueError("unrecognized reset time")
    month, day, hour, minute, am_pm = match.groups()
    hour, minute = int(hour), int(minute or 0)
    if not 1 <= hour <= 12 or not 0 <= minute < 60:
        raise ValueError("invalid reset time")
    hour = hour % 12 + (12 if am_pm.lower() == "pm" else 0)
    local = now.astimezone(tz)
    if month:
        month = dt.datetime.strptime(month, "%b").month
        # Include the preceding year for a late-Dec reset polled in early Jan.
        candidates = []
        for year in (local.year - 1, local.year, local.year + 1):
            try:
                candidate = dt.datetime(year, month, int(day), hour, minute, tzinfo=tz)
            except ValueError:
                continue
            if candidate >= local - dt.timedelta(days=2):
                candidates.append(candidate)
        if not candidates:
            raise ValueError("invalid reset date")
        candidate = min(candidates)
    else:
        candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate < local:
            candidate += dt.timedelta(days=1)
    return int(candidate.timestamp())


def parse_usage(text, now):
    buckets = []
    for line in text.splitlines():
        match = LINE.fullmatch(line.strip())
        if not match:
            continue
        kind, label, percent, when, tzname = match.groups()
        percent = int(percent)
        if not 0 <= percent <= 100:
            raise ValueError("invalid usage percentage")
        buckets.append(
            dict(
                kind=kind,
                label=label or "",
                used_percent=percent,
                resets_at=parse_reset(when, ZoneInfo(tzname), now),
                resets_text=when,
                tz=tzname,
            )
        )
    weekly = [b for b in buckets if b["kind"] == "week" and b["label"].lower() == "all models"]
    session = [b for b in buckets if b["kind"] == "session"]
    if len(weekly) != 1:
        raise ValueError("missing or ambiguous all-models weekly line")
    return dict(
        ts=int(now.timestamp()),
        source="claude-cli:/usage",
        weekly=weekly[0],
        session=session[0] if session else None,
        buckets=buckets,
    )


def publish(row, destination=OUT):
    destination.mkdir(parents=True, exist_ok=True)
    # Write history before publishing latest: any failure keeps last-ok ts intact.
    with (destination / "claude-usage.ndjson").open("a") as stream:
        stream.write(json.dumps(row, allow_nan=False) + "\n")
    with tempfile.NamedTemporaryFile(mode="w", dir=destination, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(row, stream, allow_nan=False)
    try:
        temporary.replace(destination / "claude-usage.json")
    finally:
        temporary.unlink(missing_ok=True)


def main(destination=OUT):
    executable = shutil.which(
        "claude",
        path=str(Path.home() / ".local/bin") + ":/opt/homebrew/bin:" + os.environ.get("PATH", ""),
    )
    if not executable:
        raise RuntimeError("claude executable unavailable")
    response = subprocess.run(
        [executable, "-p", "/usage"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        cwd="/tmp",
        timeout=int(os.environ.get("CLAUDE_USAGE_POLL_TIMEOUT", "60")),
    )
    if response.returncode:
        raise RuntimeError("claude /usage command failed")
    row = parse_usage(response.stdout, dt.datetime.now(dt.timezone.utc))
    publish(row, destination)
    print(
        f"claude usage: weekly={row['weekly']['used_percent']}% "
        f"resets_at={row['weekly']['resets_at']} buckets={len(row['buckets'])}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Provider errors may contain sensitive response fields; never echo them.
        print("claude usage poll FAILED; latest unchanged", file=sys.stderr)
        sys.exit(1)
