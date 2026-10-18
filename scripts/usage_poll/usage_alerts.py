"""FYI poll-health transitions; latest-file timestamps, never mtime, govern age."""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import sys
import time
from pathlib import Path

ALERT_INTERVAL_SECONDS = 6 * 3600


def timestamp(value):
    try:
        if isinstance(value, (float, int)):
            result = float(value)
        else:
            result = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def safe_message(message, name):
    """Accept only the pollers' controlled diagnostics; never echo provider text."""
    message = " ".join(message.split())
    prefix = rf"{name} usage poll FAILED; latest unchanged"
    note = r"(?:timeout \d+s|exit -?\d+|no weekly line|invalid weekly usage|command unavailable)"
    suffix = rf"(?:: (?:claude executable unavailable|try[123]: {note}(?:; try[123]: {note})*))?"
    if re.fullmatch(prefix + suffix, message):
        return message[:120]
    return "poll failed (diagnostic redacted)" if message else "none"


def update_alerts(home, log, allowance, results, *, now=None):
    now = time.time() if now is None else now
    # Inspect the last transition for each poller, not the last line overall.
    last = {}
    try:
        with log.open() as stream:
            for line in stream:
                match = re.match(r"(\S+) usage-poll: (codex|claude) (STALE|RECOVERED)\b", line)
                if match:
                    last[match[2]] = (timestamp(match[1]), match[3])
    except FileNotFoundError:
        pass
    utc = dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for name, succeeded, message in results:
        path = home / ".codex/orchestrator/usage-poll" / f"{name}-usage.json"
        try:
            ts = timestamp(json.loads(path.read_text()).get("ts"))
        except (OSError, ValueError, TypeError, AttributeError):
            ts = None
        age = now - ts if ts is not None and ts <= now else None
        age_text = f"{age:.0f}" if age is not None else "unknown"
        prior_ts, prior_state = last.get(name, (None, None))
        line = None
        if age is None or age > allowance:
            if (
                prior_state != "STALE"
                or prior_ts is None
                or now - prior_ts >= ALERT_INTERVAL_SECONDS
            ):
                line = (
                    f"{utc} usage-poll: {name} STALE age={age_text}s allowance={allowance}s "
                    f"last_error={safe_message(message, name)}"
                )
                if age is None:
                    line = line.replace("age=unknowns", "age=unknown")
        elif succeeded and prior_state == "STALE":
            line = f"{utc} usage-poll: {name} RECOVERED age={age_text}s"
        if line:
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a") as stream:
                stream.write(line + "\n")


if __name__ == "__main__":
    try:
        update_alerts(
            Path.home(),
            Path(
                os.environ.get("HANDOFF_ALERT_LOG", str(Path.home() / ".codex/handoff/alerts.log"))
            ),
            int(sys.argv[1]),
            [
                (
                    name,
                    os.environ[f"USAGE_POLL_{name.upper()}_RC"] == "0",
                    os.environ[f"USAGE_POLL_{name.upper()}_MESSAGE"],
                )
                for name in ("codex", "claude")
            ],
        )
    except OSError:
        # Digest notifications are FYI, never a new capacity/dispatch gate.
        print("usage-poll: alert log unavailable", file=sys.stderr)
