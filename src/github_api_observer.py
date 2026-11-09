"""Observe actual shared-reader GitHub requests without changing their disposition.

Only HTTP metadata is retained. Debug headers and response bodies never enter
an observation record; a logging failure never retries the underlying command.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path


def _lane():
    lane = os.environ.get("ORCH_API_LANE", "other")
    return (
        lane if lane in {"opener", "closer", "sync", "deployment", "metrics", "direct"} else "other"
    )


def observe_github(result, args, callsite):
    """Retain response metadata only; an observation failure never retries a write."""
    rows, current = [], None
    for line in (result.stderr or "").splitlines():
        request = re.match(r"^> ([A-Z]+) ([^ ?]+)(?:\?\S*)? HTTP/", line)
        if request:
            if current:
                rows.append(current)
            endpoint = request[2]
            if endpoint.lower().startswith("/repos/stranske/collab-admin"):
                current = None
                continue
            current = {
                "kind": "http",
                "method": request[1],
                "endpoint": endpoint,
                "http_status": None,
                "resource": None,
            }
        elif current:
            status = re.match(r"^< HTTP/\S+ (\d+)", line)
            header = re.match(r"^< X-Ratelimit-(\w+):\s*(\S+)", line, re.I)
            if status:
                current["http_status"] = int(status[1])
            elif header:
                key, value = header[1].lower(), header[2]
                if key in {"limit", "remaining", "reset", "used"} and value.isdecimal():
                    current[key] = int(value)
                elif key == "resource":
                    current[key] = value
    if current:
        rows.append(current)
    if len(rows) == 1 and args[1:3] == ["api", "graphql"]:
        try:
            cost = json.loads(result.stdout).get("data", {}).get("rateLimit", {}).get("cost")
            if type(cost) is int:
                rows[0]["graphql_cost"] = cost
        except (ValueError, AttributeError):
            pass
    if not rows:
        rows = [{"kind": "call", "coverage": "unknown", "http_requests_observed": 0}]
    call_id = uuid.uuid4().hex
    path = Path(
        os.environ.get(
            "LANE_API_EVENTS", str(Path.home() / ".codex/orchestrator/github-api/events.jsonl")
        )
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("a") as stream:
            os.chmod(path, 0o600)
            fcntl.flock(stream, fcntl.LOCK_EX)
            for row in rows:
                stream.write(
                    json.dumps(
                        {
                            **row,
                            "lane": _lane(),
                            "component": "shared-gh-reader",
                            "operation": " ".join(args[1:3]),
                            "call_id": call_id,
                            "exit_code": result.returncode,
                            "observed_at": datetime.now().astimezone().isoformat(),
                            "callsite": callsite,
                            "coverage": row.get(
                                "coverage", "partial" if result.returncode == 124 else "complete"
                            ),
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        result.api_observation_error = type(exc).__name__
        print(
            "shared GitHub reader API attribution unavailable: " + type(exc).__name__,
            file=sys.stderr,
        )
    # Debug may contain private bodies and headers. Only ordinary CLI errors return.
    errors = re.findall(r"(?m)^(?:gh:|GraphQL:|HTTP \d+).*", result.stderr or "")
    result.stderr = re.sub(r"gh[hopsu]_[A-Za-z0-9_]+", "REDACTED", "\n".join(errors))
    if result.returncode and not result.stderr:
        result.stderr = f"gh failed (exit {result.returncode}); see observed HTTP metadata"
    result.http_events = rows
    return result


def run_gh(args, *, timeout_s, callsite):
    command = ["gh", *args]
    environment = dict(os.environ)
    environment["GH_DEBUG"] = "api"
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:

        def decode(value):
            return value.decode(errors="replace") if isinstance(value, bytes) else value or ""

        partial = observe_github(
            subprocess.CompletedProcess(command, 124, decode(exc.stdout), decode(exc.stderr)),
            command,
            callsite,
        )
        exc.stderr = partial.stderr
        raise
    return observe_github(result, command, callsite)
