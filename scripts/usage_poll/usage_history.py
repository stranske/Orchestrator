"""Bounded poll histories, shared by the installed standalone poller scripts."""

import json
import sys
import tempfile
from collections import deque
from pathlib import Path

HISTORY_SECONDS = 14 * 86400 + 1800  # Keep the latest completed period and its boundary poll.
HISTORY_MAX_LINES = 4096
HISTORY_MAX_BYTES = 8 * 1024 * 1024


def append_history(path, row):
    """Atomically append and cap; report every discarded line without its contents."""
    retained = deque()
    size = rotated = 0
    cutoff = row["ts"] - HISTORY_SECONDS

    def consider(line):
        nonlocal size, rotated
        try:
            ts = json.loads(line).get("ts")
            keep = isinstance(ts, (int, float)) and ts >= cutoff
        except (ValueError, AttributeError):
            keep = False
        if not keep:
            rotated += 1
            return
        retained.append(line)
        size += len(line)
        while len(retained) > HISTORY_MAX_LINES or size > HISTORY_MAX_BYTES:
            size -= len(retained.popleft())
            rotated += 1

    if path.exists():
        with path.open("rb") as stream:
            for line in stream:
                consider(line)
    consider((json.dumps(row, allow_nan=False) + "\n").encode())
    with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.writelines(retained)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"{path.name}: rotated_out={rotated} retained={len(retained)} lines", file=sys.stderr)
