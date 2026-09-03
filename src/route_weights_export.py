#!/usr/bin/env python3
"""Export conservative route-weight rankings for a fail-open remote consumer.

The learner remains local.  This module makes a versioned, thresholded snapshot
available for a consumer that deliberately falls back to its own static policy
whenever the export is missing, malformed, stale, or insufficiently evidenced.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import capabilities
import feedback
import provision
import router

SCHEMA = "orchestrator.route-weights/v1"
DEFAULT_MIN_OBSERVATIONS = 20
EXPORT_BRANCH = "exports/route-weights"
EXPORT_PATH = Path("config/route-weights.json")
# This is intentionally smaller than ROUTE_TABLE.  It is the contract that the
# Workflows keepalive policy may act on; adding a router task type here is an
# explicit cross-repository consumer-contract change, not an accidental export.
CONSUMER_TASK_TYPES = (
    "implement",
    "review",
    "testgen",
    "mechanical",
    "codemod",
    "cross_repo",
)


def _now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


def _read_rows(db_path: Path) -> tuple[int, list[sqlite3.Row]]:
    """Return the latest legacy route-weight version without creating/migrating a DB."""
    if not db_path.exists():
        return 0, []
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='route_weights'"
        ).fetchone()
        if not exists:
            return 0, []
        version = int(
            connection.execute("SELECT COALESCE(MAX(version), 0) FROM route_weights").fetchone()[0]
        )
        if version == 0:
            return 0, []
        rows = connection.execute(
            "SELECT task_type, agent, posterior, n_obs, success_rate "
            "FROM route_weights WHERE version=?",
            (version,),
        ).fetchall()
        return version, rows
    finally:
        connection.close()


def _ranking(rows: list[sqlite3.Row], minimum: int) -> list[dict[str, Any]]:
    ranking = [
        {
            "agent": str(row["agent"]),
            "posterior": float(row["posterior"]),
            "n_obs": int(row["n_obs"]),
            "success_rate": float(row["success_rate"]),
        }
        for row in rows
        if int(row["n_obs"]) >= minimum
    ]
    return sorted(ranking, key=lambda row: (-row["posterior"], -row["success_rate"], row["agent"]))


def build_document(db_path: Path, minimum: int = DEFAULT_MIN_OBSERVATIONS) -> dict[str, Any]:
    """Build a deterministic public snapshot from only the latest weight version."""
    if minimum < 1:
        raise ValueError("min_observations must be at least 1")
    missing = sorted(set(CONSUMER_TASK_TYPES) - set(router.ROUTE_TABLE))
    if missing:
        raise RuntimeError(
            f"consumer task types absent from router.ROUTE_TABLE: {', '.join(missing)}"
        )

    version, rows = _read_rows(db_path)
    by_task: dict[str, list[sqlite3.Row]] = {task_type: [] for task_type in CONSUMER_TASK_TYPES}
    for row in rows:
        task_type = str(row["task_type"])
        if task_type in by_task:
            by_task[task_type].append(row)

    task_types: dict[str, dict[str, Any]] = {}
    reserve: dict[str, list[dict[str, Any]]] = {}
    for task_type in CONSUMER_TASK_TYPES:
        routable = [
            row for row in by_task[task_type] if str(row["agent"]) not in router.RESERVE_AGENTS
        ]
        reserve_rows = [
            row for row in by_task[task_type] if str(row["agent"]) in router.RESERVE_AGENTS
        ]
        ranking = _ranking(routable, minimum)
        # A task with no eligible observed row stays explicit and false: absence
        # of evidence must not resemble a zero-valued preference.
        task_types[task_type] = {"ranking": ranking, "evidence_ok": bool(ranking)}
        reserve_ranking = _ranking(reserve_rows, minimum)
        if reserve_ranking:
            reserve[task_type] = reserve_ranking

    return {
        "schema": SCHEMA,
        "generated_at": _now(),
        "source_version": version,
        "min_observations": minimum,
        "task_types": task_types,
        "reserve": reserve,
    }


def _canonical_bytes(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_document(path: Path, document: dict[str, Any]) -> bool:
    """Write only semantic changes, retaining the earlier timestamp on an unchanged export."""
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = None
        if isinstance(existing, dict):
            old_semantic = {key: value for key, value in existing.items() if key != "generated_at"}
            new_semantic = {key: value for key, value in document.items() if key != "generated_at"}
            if old_semantic == new_semantic:
                return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(_canonical_bytes(document))
    temporary.replace(path)
    return True


def publish_document(document: dict[str, Any]) -> bool:
    """Commit and push the export branch only when its remote artifact changes."""
    canonical = provision.ensure_canonical("stranske/Orchestrator")
    subprocess.run(["git", "-C", str(canonical), "fetch", "--prune", "origin"], check=True)
    remote_ref = f"origin/{EXPORT_BRANCH}"
    has_remote = (
        subprocess.run(
            ["git", "-C", str(canonical), "rev-parse", "--verify", "--quiet", remote_ref],
            check=False,
        ).returncode
        == 0
    )
    if has_remote:
        has_local = (
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(canonical),
                    "show-ref",
                    "--verify",
                    "--quiet",
                    f"refs/heads/{EXPORT_BRANCH}",
                ],
                check=False,
            ).returncode
            == 0
        )
        if has_local:
            subprocess.run(["git", "-C", str(canonical), "switch", EXPORT_BRANCH], check=True)
            subprocess.run(
                ["git", "-C", str(canonical), "merge", "--ff-only", remote_ref], check=True
            )
        else:
            subprocess.run(
                ["git", "-C", str(canonical), "switch", "--track", "-c", EXPORT_BRANCH, remote_ref],
                check=True,
            )
    else:
        subprocess.run(
            ["git", "-C", str(canonical), "switch", "-c", EXPORT_BRANCH, "origin/main"], check=True
        )
    target = canonical / EXPORT_PATH
    if not write_document(target, document):
        print("unchanged")
        return False
    subprocess.run(["git", "-C", str(canonical), "add", str(EXPORT_PATH)], check=True)
    subprocess.run(
        ["git", "-C", str(canonical), "commit", "-m", "chore: export route weights"], check=True
    )
    subprocess.run(
        ["git", "-C", str(canonical), "push", "origin", f"HEAD:{EXPORT_BRANCH}"], check=True
    )
    return True


def _fixture_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE route_weights (version INTEGER, ts INTEGER, task_type TEXT, agent TEXT, "
            "prior REAL, posterior REAL, n_obs INTEGER, success_rate REAL, cost_per_success REAL, "
            "score REAL, rationale TEXT, win_start INTEGER, win_end INTEGER)"
        )
        rows = [
            (1, "implement", "cursor", 0.50, 12, 0.50),
            (2, "implement", "codex", 0.90, 25, 0.88),
            (2, "implement", "claude", 0.99, 31, 0.95),
            (2, "implement", "gemini", 0.40, 19, 0.40),
            (2, "unknown", "codex", 0.99, 100, 1.00),
        ]
        connection.executemany(
            "INSERT INTO route_weights VALUES (?,0,?,?,0,?,?,?,0,0,'fixture',0,0)", rows
        )
        connection.commit()
    finally:
        connection.close()


def _selftest() -> None:
    with tempfile.TemporaryDirectory(prefix="route-weights-export-") as directory:
        root = Path(directory)
        db_path = root / "fixture.db"
        _fixture_db(db_path)
        document = build_document(db_path, minimum=20)
        assert document["schema"] == SCHEMA, document
        assert document["source_version"] == 2, document
        ranking = document["task_types"]["implement"]["ranking"]
        assert [row["agent"] for row in ranking] == ["codex"], ranking
        assert all(row["n_obs"] >= 20 for row in ranking), ranking
        assert document["task_types"]["implement"]["evidence_ok"], document
        assert document["task_types"]["review"]["evidence_ok"] is False, document
        assert document["reserve"]["implement"][0]["agent"] == "claude", document
        assert "unknown" not in document["task_types"], document
        output = root / "route-weights-export.json"
        assert write_document(output, document) is True
        assert write_document(output, build_document(db_path, minimum=20)) is False
    print("route_weights_export.py selftest: OK")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-observations", type=int, default=DEFAULT_MIN_OBSERVATIONS)
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.environ.get("ORCH_STATE_DIR", Path.home() / ".codex" / "orchestrator")),
    )
    parser.add_argument("--feedback-db", type=Path, default=feedback.DB_PATH)
    parser.add_argument(
        "--publish", action="store_true", help="publish only with ORCH_ROUTE_WEIGHTS_PUBLISH=1"
    )
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        _selftest()
        return 0
    document = build_document(args.feedback_db, args.min_observations)
    target = args.state_dir / "route-weights-export.json"
    changed = write_document(target, document)
    print(f"{'wrote' if changed else 'unchanged'} {target}")
    capabilities.daily_heartbeat(
        "route-weights-export",
        "success",
        ref=str(target),
        metadata={"source_version": document["source_version"], "changed": changed},
    )
    if args.publish:
        if os.environ.get("ORCH_ROUTE_WEIGHTS_PUBLISH") != "1":
            print("publish blocked: ORCH_ROUTE_WEIGHTS_PUBLISH!=1; local export retained")
        else:
            publish_document(document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
