#!/usr/bin/env python3
"""Read-only harness for research_scheduler.build_research_plan (exercise fixture)."""
from __future__ import annotations

import json
import random
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

import feedback  # noqa: E402
import research_scheduler  # noqa: E402
import research_subjects  # noqa: E402

FIXTURE = Path(__file__).resolve().parent
OUT = FIXTURE / "plan-out.json"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(feedback.SCHEMA)
    feedback._migrate_schema(conn)
    research_subjects.ensure_schema(conn)
    return conn


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "pass"
    capacity = json.loads((FIXTURE / "capacity.json").read_text())
    items = json.loads((FIXTURE / "items.json").read_text())
    hyps = json.loads((FIXTURE / "hypotheses.json").read_text())
    learned = {
        "implement": {
            "cursor": {"posterior": 0.55, "n_obs": 1},
            "codex": {"posterior": 0.80, "n_obs": 4},
            "claude": {"posterior": 0.50, "n_obs": 0},
        }
    }
    conn = _conn()
    kwargs = {
        "learned": learned,
        "hyps": hyps,
        "claimed_targets": set(),
        "rng": random.Random(0),
        "conn": conn,
        "unevaluated_cap": 99,
    }
    if mode == "break":
        kwargs["production_reserve"] = {agent: 99 for agent in capacity["agents"]}
    plan = research_scheduler.build_research_plan(items, capacity, **kwargs)
    OUT.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": plan.get("status"), "planned_count": len(plan.get("planned") or [])}))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
