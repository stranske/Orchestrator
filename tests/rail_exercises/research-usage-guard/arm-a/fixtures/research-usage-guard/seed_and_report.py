#!/usr/bin/env python3
"""Isolated research-usage-guard admission + report exercise harness."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

import feedback  # noqa: E402
import research_usage_guard  # noqa: E402

FIXTURE = Path(__file__).resolve().parent
OUT = FIXTURE / "guard-out.json"


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "pass"
    db_path = FIXTURE / "guard.db"
    if db_path.exists():
        db_path.unlink()
    os.environ["ORCH_FEEDBACK_DB"] = str(db_path)
    os.environ.pop("ORCH_CAPABILITY_HEARTBEATS", None)
    feedback.DB_PATH = db_path
    conn = sqlite3.connect(str(db_path))
    research_usage_guard.ensure_schema(conn)
    now = int(time.time())
    enabled = {"ORCH_RESEARCH_ARM": "1"}
    admitted = research_usage_guard.assess_and_record_opportunity(
        exp_id="tick-1700000000-exercise",
        repo="owner/repo",
        subject="exercise-subject",
        spec_text="spec text",
        base_sha="sha1",
        candidate_diffs={"codex": "diff1"},
        evaluator_agents=["vibe"],
        env=enabled,
        conn=conn,
        now=now,
    )
    if mode == "break":
        duplicate = research_usage_guard.assess_and_record_opportunity(
            exp_id="tick-1700000001-exercise",
            repo="owner/repo",
            subject="exercise-subject",
            spec_text="spec text",
            base_sha="sha1",
            candidate_diffs={"codex": "diff1"},
            evaluator_agents=["vibe"],
            env=enabled,
            conn=conn,
            now=now + 10,
        )
        payload = {"admitted": admitted, "duplicate": duplicate}
    else:
        report = research_usage_guard.generate_usage_report(conn=conn, now=now + 50)
        payload = {"admitted": admitted, "report": report}
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    conn.close()
    print(json.dumps({"mode": mode, "decision": payload["admitted"]["decision"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
