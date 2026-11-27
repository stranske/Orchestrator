#!/usr/bin/env python3
"""Muse's work ladder: simple work first, harder work as it succeeds (owner, 2026-11-27, campaign P3).

Muse has been a LATE implement seat since 2026-11-11: the router draws it only when no non-late seat
has capacity, which in practice is never, so it collected 17 implement runs in two weeks and the
comparative LP (20 judged runs minimum) could not rank it. The owner's rule: start it on simpler work
when available and raise the level as it succeeds, so its record says where it succeeds and fails.

Rungs (the work, not the agent):
  1  `mechanical` work, and `implement` issues with 1-2 tasks
  2  `implement` issues with 3-4 tasks
  3  any larger `implement` issue
Unknown size never lifts Muse: it stays late, as before. Measured demand over 14 days to 2026-11-27:
rung 1 = 61 mechanical runs + 20 small issues, rung 2 = 105 issues, rung 3 = 16 issues.

Muse's LEVEL is computed from the Brain on every read; nothing is stored. Level 1 is always open.
Level L+1 opens when Muse has >= PROMOTE_MIN_RESOLVED resolved outcomes (durable or broke_later) on
rung L within WINDOW_DAYS, at least PROMOTE_MIN_DURABLE of them durable. A rung whose rate falls
below the bar closes the rungs above it again. On work at or below its level, Muse joins the ordinary
draw (it stops being late); its share there is the tilt's floor, since the LP cannot price it yet.

Gate contract (latched-gate check): what opens a rung is Muse's own resolved outcomes on the rung
below; rung 1 is never closed, so that evidence can always accrue; the population measured (Muse runs
the router recorded at a rung) is exactly the population the open rung produces; `line()` prints the
level, each rung's resolved count against the bar, and its durable rate. An unreadable Brain is
`unmeasured` and keeps level 1 (fail toward motion, never toward a higher rung).
Kill switch: ORCH_MUSE_LADDER=0 (Muse stays a late seat everywhere).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

LADDER_AGENT = "muse"
KILL_SWITCH = "ORCH_MUSE_LADDER"
PROMOTE_MIN_RESOLVED = 5
PROMOTE_MIN_DURABLE = 0.80
WINDOW_DAYS = 90
TOP_RUNG = 3
IMPLEMENT_KINDS = frozenset({"implement", "implementation", "coding"})
# Judged, terminal outcomes: durable counts for Muse; broke_later and the Brain's regression values
# (feedback.CAPABILITY_REGRESSION_DURABILITY: reverted, reworked, reopened) count against it.
RESOLVED_FAILURES = frozenset({"broke_later", "reverted", "reworked", "reopened"})
CACHE_SECONDS = 300
_CACHE: dict[str, tuple[float, dict]] = {}


def enabled(env: Mapping[str, str] | None = None) -> bool:
    source: Mapping[str, str] = os.environ if env is None else env
    return str(source.get(KILL_SWITCH, "1")).strip().lower() not in {"0", "false", "no", "off"}


def rung_for(task_type: str, size_tasks: int | None) -> int | None:
    """The rung of a piece of work, or None when its size is unknown (never lifts Muse)."""
    if task_type == "mechanical":
        return 1
    if task_type not in IMPLEMENT_KINDS or size_tasks is None or size_tasks < 0:
        return None
    return 1 if size_tasks <= 2 else 2 if size_tasks <= 4 else 3


def _db_path() -> Path:
    import feedback  # noqa: PLC0415

    return Path(feedback.DB_PATH)


def standing(db_path: Path | None = None, now: int | None = None, *, fresh: bool = False) -> dict:
    """{level, rungs: {r: {resolved, durable, rate}}, state: measured|unmeasured, reason}."""
    # Absolute before the cache key and the URI: ORCH_FEEDBACK_DB may be relative, and
    # Path.as_uri() raises ValueError on a relative path (outside the sqlite3.Error fallback).
    path = (Path(db_path) if db_path is not None else _db_path()).expanduser().resolve()
    now = int(time.time()) if now is None else now
    key = str(path)
    if not fresh and key in _CACHE and time.time() - _CACHE[key][0] < CACHE_SECONDS:
        return _CACHE[key][1]
    rungs: dict[int, dict[str, Any]] = {
        r: {"resolved": 0, "durable": 0, "rate": None} for r in range(1, TOP_RUNG + 1)
    }
    try:
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as conn:
            rows = conn.execute(
                "SELECT json_extract(r.routing_metadata,'$.route_decision.muse_ladder.rung'), "
                "o.durability FROM runs r LEFT JOIN outcomes o USING(run_id) "
                "WHERE r.agent=? AND r.ts>=? AND "
                "json_extract(r.routing_metadata,'$.route_decision.muse_ladder.rung') IS NOT NULL",
                (LADDER_AGENT, now - WINDOW_DAYS * 86400),
            ).fetchall()
    except sqlite3.Error as exc:
        out = {
            "level": 1,
            "rungs": rungs,
            "state": "unmeasured",
            "reason": f"Brain unreadable ({exc}); level 1 kept",
        }
        _CACHE[key] = (time.time(), out)
        return out
    for rung, durability in rows:
        try:
            r = int(rung)
        except (TypeError, ValueError):
            continue
        if r in rungs and (durability == "durable" or durability in RESOLVED_FAILURES):
            rungs[r]["resolved"] += 1
            rungs[r]["durable"] += durability == "durable"
    for row in rungs.values():
        row["rate"] = row["durable"] / row["resolved"] if row["resolved"] else None
    level = 1
    for r in range(1, TOP_RUNG):
        row = rungs[r]
        if row["resolved"] >= PROMOTE_MIN_RESOLVED and row["rate"] >= PROMOTE_MIN_DURABLE:
            level = r + 1
        else:
            break
    out = {"level": level, "rungs": rungs, "state": "measured", "reason": None}
    _CACHE[key] = (time.time(), out)
    return out


def decision(
    task_type: str, size_tasks: int | None, env: Mapping[str, str] | None = None
) -> dict | None:
    """What the router records and uses: {rung, level, lifted}, or None when the work has no rung."""
    rung = rung_for(task_type, size_tasks)
    if rung is None:
        return None
    if not enabled(env):
        return {"rung": rung, "level": None, "lifted": False, "reason": f"{KILL_SWITCH}=0"}
    stand = standing()
    return {"rung": rung, "level": stand["level"], "lifted": rung <= stand["level"]}


def line(stand: dict | None = None) -> str:
    stand = standing() if stand is None else stand
    parts = []
    for r, row in stand["rungs"].items():
        rate = "-" if row["rate"] is None else f"{row['rate']:.0%}"
        parts.append(f"rung {r} {row['resolved']}/{PROMOTE_MIN_RESOLVED} resolved, durable {rate}")
    tail = f" ({stand['reason']})" if stand.get("reason") else ""
    return (
        f"muse ladder: level {stand['level']} of {TOP_RUNG}; "
        + "; ".join(parts)
        + f"; next rung opens at >= {PROMOTE_MIN_RESOLVED} resolved, >= {PROMOTE_MIN_DURABLE:.0%} durable"
        + tail
    )


def main(argv: list[str]) -> int:
    stand = standing(fresh=True)
    if "--json" in argv:
        print(json.dumps(stand))
    else:
        print(line(stand))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
