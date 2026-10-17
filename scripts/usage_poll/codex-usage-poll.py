#!/usr/bin/env python3
"""Read-only Codex app-server usage poll. Pure parser never retains identifiers.

The only RPCs are initialize and account/rateLimits/read, plus initialized notice.
A command/parse failure exits non-zero without publishing a new last-ok timestamp.
"""

import json
import math
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

OUT = Path.home() / ".codex/orchestrator/usage-poll"
TIMEOUT = float(os.environ.get("CODEX_USAGE_POLL_TIMEOUT", "40"))


def window(data):
    if data is None:
        return None
    percent, minutes, end = (data.get(k) for k in ("usedPercent", "windowDurationMins", "resetsAt"))
    if (
        not isinstance(percent, (int, float))
        or not 0 <= percent <= 100
        or not isinstance(end, (int, float))
        or not math.isfinite(end)
        or not isinstance(minutes, int)
        or minutes <= 0
    ):
        raise ValueError("invalid rate-limit window")
    return dict(used_percent=percent, window_minutes=minutes, resets_at=end)


def parse_response(response, now):
    limits = (
        (response.get("rateLimitsByLimitId") or {}).get("codex") or response.get("rateLimits") or {}
    )
    primary = window(limits.get("primary"))
    if not primary or primary["window_minutes"] != 10080:
        raise ValueError("weekly primary missing")
    reset = response.get("rateLimitResetCredits") or {}
    raw_credits = reset.get("credits")
    credits = None
    if raw_credits is not None:
        credits = []
        for credit in raw_credits:
            expiry = credit.get("expiresAt")
            if (
                not isinstance(expiry, (int, float))
                or not math.isfinite(expiry)
                or not isinstance(credit.get("status"), str)
            ):
                raise ValueError("invalid reset credit")
            # Status and expiry are sufficient; never persist IDs or balances.
            credits.append(dict(expires_at=expiry, status=credit["status"]))
    return dict(
        ts=int(now),
        source="codex-app-server:account/rateLimits/read",
        primary=primary,
        secondary=window(limits.get("secondary")),
        rate_limit_reached_type=limits.get("rateLimitReachedType"),
        ordinary_usage_allowed=response.get("ordinaryUsageAllowed"),
        reset_credits_available=reset.get("availableCount"),
        reset_credits=credits,
    )


def read_response():
    executable = shutil.which("codex", path="/opt/homebrew/bin:" + os.environ.get("PATH", ""))
    if not executable:
        raise RuntimeError("codex executable unavailable")
    process = subprocess.Popen(
        [executable, "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )

    def send(message):
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    def receive(wanted):
        end = time.monotonic() + TIMEOUT
        while time.monotonic() < end:
            ready, _, _ = select.select(
                [process.stdout], [], [], min(1.0, max(0, end - time.monotonic()))
            )
            if not ready:
                continue
            line = process.stdout.readline()
            if not line:
                raise RuntimeError("app-server closed stdout")
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") == wanted:
                if "error" in message:
                    raise RuntimeError("app-server RPC failed")
                return message.get("result")
        raise RuntimeError("app-server response timeout")

    try:
        send(
            dict(
                id=1,
                method="initialize",
                params=dict(
                    clientInfo=dict(
                        name="orchestrator-usage-poll", title="Orchestrator usage poll", version="1"
                    )
                ),
            )
        )
        receive(1)
        send(dict(method="initialized"))
        send(dict(id=2, method="account/rateLimits/read", params=None))
        return receive(2)
    finally:
        process.stdin.close()
        process.terminate()
        try:
            process.wait(5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def publish(row, destination=OUT):
    destination.mkdir(parents=True, exist_ok=True)
    with (destination / "codex-usage.ndjson").open("a") as stream:
        stream.write(json.dumps(row, allow_nan=False) + "\n")
    with tempfile.NamedTemporaryFile(mode="w", dir=destination, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(row, stream, allow_nan=False)
    try:
        temporary.replace(destination / "codex-usage.json")
    finally:
        temporary.unlink(missing_ok=True)


def main(destination=OUT):
    row = parse_response(read_response(), time.time())
    publish(row, destination)
    print(
        f"codex usage: weekly={row['primary']['used_percent']}% "
        f"resets_at={row['primary']['resets_at']} "
        f"reset_credits={row['reset_credits_available']}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("codex usage poll FAILED; latest unchanged", file=sys.stderr)
        sys.exit(1)
