#!/usr/bin/env python3
"""Export conservative route-weight rankings for a fail-open remote consumer.

The learner remains local.  This module makes a versioned, thresholded snapshot
available for a consumer that deliberately falls back to its own static policy
whenever the export is missing, malformed, stale, or insufficiently evidenced.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import capabilities
import capacity
import exploration_collection
import feedback
import provision
import router

# Workflows agent_delegation_policy.js accepts only this string; bumping to /v2
# would fail-open away from learned weights. Capacity is additive via CAPACITY_SCHEMA.
SCHEMA = "orchestrator.route-weights/v1"
CAPACITY_SCHEMA = "orchestrator.route-weights-capacity/v1"
DEFAULT_MIN_OBSERVATIONS = 20
EXPORT_BRANCH = "exports/route-weights"
EXPORT_PATH = Path("config/route-weights.json")
WEEK_SECONDS = 7 * 86400
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
            "SELECT task_type, agent, posterior, score, n_obs, success_rate "
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


def _current_capacity_snapshot() -> dict[str, Any]:
    """Fresh in-process capacity snapshot for the export.

    Always call ``capacity.build()`` — never the tick-written handoff JSON. That
    artifact can lag a shed-marker flip or pool change; a standalone export must
    observe current inputs so a state change still triggers publication.
    """
    return capacity.build()


def _capacity_block(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """Per-agent capacity state for remote consumers (state and window only)."""
    if snapshot is None:
        snapshot = _current_capacity_snapshot()
    agents_raw = snapshot.get("agents")
    agents = agents_raw if isinstance(agents_raw, dict) else {}
    observed_raw = snapshot.get("generated_at")
    try:
        observed_at = int(observed_raw) if observed_raw is not None else 0
    except (TypeError, ValueError, OverflowError):
        observed_at = 0
    block: dict[str, Any] = {}
    for agent in sorted(capacity.AGENTS):
        raw = agents.get(agent)
        row = raw if isinstance(raw, dict) else {}
        cfg = capacity.AGENTS[agent]
        state = row.get("state")
        if not isinstance(state, str) or state not in capacity.AGENT_STATES:
            state = capacity.UNKNOWN
        window = row.get("window")
        if not isinstance(window, str) or not window:
            window = str(cfg.get("window") or "")
        block[agent] = {
            "state": state,
            "window": window,
            "observed_at": observed_at,
        }
    return block


def _exploration_block(
    version: int,
    by_task: dict[str, list[sqlite3.Row]],
    task_types: dict[str, dict[str, Any]],
    timestamp: int,
) -> dict[str, Any]:
    week = timestamp // WEEK_SECONDS
    # The public export balances arms by week, without consulting the local
    # supervised-window deficits (which could change the arm within a week).
    mode = exploration_collection.choose_mode({}, now=timestamp)
    eligible = router.KEEPALIVE_AGENTS - router.RESERVE_AGENTS - router.BACKUP_AGENTS
    capacity = {"agents": {agent: {"state": "ok"} for agent in eligible}}
    challengers: dict[str, str | None] = {}
    for task_type in CONSUMER_TASK_TYPES:
        ranking = task_types[task_type]["ranking"]
        ranks = {row["agent"]: index for index, row in enumerate(ranking)}
        # Exploitation must agree with the consumer's thresholded ranking.
        # Low-observation rows still inform challenger selection, so exploration
        # can collect evidence from seats absent from the public ranking.
        rows = sorted(
            (row for row in by_task[task_type] if row["agent"] in eligible),
            key=lambda row: (
                ranks.get(row["agent"], len(ranks)),
                -float(row["posterior"]),
                -float(row["success_rate"]),
                row["agent"],
            ),
        )
        learned = {
            str(row["agent"]): {
                "rank": index,
                "posterior": row["posterior"],
                "score": row["score"],
                "n_obs": row["n_obs"],
            }
            for index, row in enumerate(rows)
        }
        pick = router.select_agent(
            task_type,
            capacity,
            only=eligible,
            learned=learned or None,
            exploration_rate=1.0,
            exploration_mode=mode,
            rng=random.Random(f"route-exploration/v1:{week}:{version}:{mode}:{task_type}"),
            simulate=True,
            profile_transport="remote",
        )
        # Never describe the exploitation winner as a challenger when the
        # router cannot find another eligible seat in the same policy tier.
        challengers[task_type] = pick["agent"] if pick and pick["exploration"] else None
    return {
        "mode": mode,
        "rate": router.EXPLORATION_RATE_DEFAULT,
        "week_start": week * WEEK_SECONDS,
        "challengers": challengers,
    }


def build_document(
    db_path: Path,
    minimum: int = DEFAULT_MIN_OBSERVATIONS,
    *,
    now: int | None = None,
    capacity_snapshot: dict[str, Any] | None = None,
    include_capacity: bool = True,
) -> dict[str, Any]:
    """Build a snapshot with repeatable samples for this week and weight version."""
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

    generated_at = (
        _now()
        if now is None
        else dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat().replace("+00:00", "Z")
    )
    timestamp = int(dt.datetime.fromisoformat(generated_at.replace("Z", "+00:00")).timestamp())
    document: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "source_version": version,
        "min_observations": minimum,
        "task_types": task_types,
        "reserve": reserve,
        "exploration": _exploration_block(version, by_task, task_types, timestamp),
    }
    if include_capacity:
        document["capacity_schema"] = CAPACITY_SCHEMA
        document["capacity"] = _capacity_block(capacity_snapshot)
    # Bind the tag to the exported policy, including rankings and the sampled
    # challengers. Capacity is additive under v1 and must not move this identity.
    routing_semantic = {
        key: value
        for key, value in document.items()
        if key not in ("generated_at", "capacity", "capacity_schema")
    }
    digest = hashlib.sha256(_canonical_bytes(routing_semantic)).hexdigest()
    document["exploration"]["policy_version"] = f"route-exploration/v1:{digest}"
    return document


def _canonical_bytes(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _semantic_for_compare(document: dict[str, Any]) -> dict[str, Any]:
    """Compare export semantics without timestamp-only churn.

    Dictionary capacity rows drop ``observed_at`` so a tick-only refresh does not
    republish. Non-dict or otherwise malformed rows are kept so the next write
    can repair them instead of comparing as equal after they were filtered out.
    """
    semantic = {key: value for key, value in document.items() if key != "generated_at"}
    capacity_block = semantic.get("capacity")
    if isinstance(capacity_block, dict):
        compared: dict[str, Any] = {}
        for agent, entry in capacity_block.items():
            if isinstance(entry, dict):
                compared[agent] = {
                    key: value for key, value in entry.items() if key != "observed_at"
                }
            else:
                compared[agent] = entry
        semantic["capacity"] = compared
    return semantic


def write_document(path: Path, document: dict[str, Any]) -> bool:
    """Write only semantic changes, retaining the earlier timestamp on an unchanged export."""
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = None
        if isinstance(existing, dict):
            if _semantic_for_compare(existing) == _semantic_for_compare(document):
                return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(_canonical_bytes(document))
    temporary.replace(path)
    return True


def publish_guard(
    new_doc: dict[str, Any], published_doc: dict[str, Any] | None
) -> tuple[bool, str]:
    """Refuse an empty learner or loss of any previously published task evidence."""
    if new_doc.get("source_version", 0) <= 0:
        return False, "source_version must be > 0 (empty or wrong feedback DB)"
    tasks = new_doc.get("task_types", {})
    if not any(entry.get("evidence_ok") is True for entry in tasks.values()):
        return False, "all task_types have evidence_ok=false"
    previous_tasks = (published_doc or {}).get("task_types", {})
    lost = sorted(
        task
        for task, entry in previous_tasks.items()
        if entry.get("evidence_ok") is True
        and tasks.get(task, {}).get("evidence_ok") is not True
    )
    if lost:
        return False, f"published evidence_ok=true became false for: {', '.join(lost)}"
    return True, ""


def publish_document(
    document: dict[str, Any], *, allow_evidence_regression: bool = False
) -> bool:
    """Commit and push the export branch only when its remote artifact changes."""
    ok, reason = publish_guard(document, None)
    if not ok and not allow_evidence_regression:
        raise ValueError(f"publish_guard refused: {reason}")
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
    published = None
    if has_remote:
        result = subprocess.run(
            ["git", "-C", str(canonical), "show", f"{remote_ref}:{EXPORT_PATH}"],
            check=True,
            capture_output=True,
            text=True,
        )
        published = json.loads(result.stdout)
    ok, reason = publish_guard(document, published)
    if not ok:
        if not allow_evidence_regression:
            raise ValueError(f"publish_guard refused: {reason}")
        print(f"publish_guard: deliberate evidence regression allowed: {reason}", file=sys.stderr)
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
        fixed_now = 100
        capacity_snapshot = capacity.build()
        document = build_document(
            db_path, minimum=20, now=fixed_now, capacity_snapshot=capacity_snapshot
        )
        assert document["schema"] == SCHEMA, document
        assert document["capacity_schema"] == CAPACITY_SCHEMA, document
        assert set(document["capacity"]) == set(capacity.AGENTS), document
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
        assert (
            write_document(
                output,
                build_document(
                    db_path, minimum=20, now=fixed_now, capacity_snapshot=capacity_snapshot
                ),
            )
            is False
        )
        # State and window each republish independently; observed_at alone does not.
        shed_snapshot = json.loads(json.dumps(capacity_snapshot))
        shed_agent = next(iter(capacity.AGENTS))
        current_state = shed_snapshot["agents"][shed_agent]["state"]
        shed_snapshot["agents"][shed_agent]["state"] = (
            capacity.OK if current_state == capacity.SHED else capacity.SHED
        )
        assert (
            write_document(
                output,
                build_document(db_path, minimum=20, now=fixed_now, capacity_snapshot=shed_snapshot),
            )
            is True
        )
        window_snapshot = json.loads(json.dumps(shed_snapshot))
        window_snapshot["agents"][shed_agent]["window"] = "1h"
        assert (
            write_document(
                output,
                build_document(
                    db_path, minimum=20, now=fixed_now, capacity_snapshot=window_snapshot
                ),
            )
            is True
        )
        observed_only = json.loads(json.dumps(window_snapshot))
        observed_only["generated_at"] = int(observed_only.get("generated_at") or 0) + 3600
        assert (
            write_document(
                output,
                build_document(db_path, minimum=20, now=fixed_now, capacity_snapshot=observed_only),
            )
            is False
        )
    print("route_weights_export.py selftest: OK")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-observations", type=int, default=DEFAULT_MIN_OBSERVATIONS)
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.environ.get("ORCH_STATE_DIR", Path.home() / ".codex" / "orchestrator")),
    )
    parser.add_argument("--feedback-db", type=Path, help="override the shared feedback DB resolver")
    parser.add_argument(
        "--publish", action="store_true", help="publish only with ORCH_ROUTE_WEIGHTS_PUBLISH=1"
    )
    parser.add_argument(
        "--allow-evidence-regression",
        action="store_true",
        help="deliberately allow an owner-approved evidence downgrade (logged)",
    )
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        _selftest()
        return 0
    db_path = feedback.resolve_db_path(args.feedback_db)
    # The run is credited as an INVOCATION before the work, and the `success` below records its
    # result. A success alone moves `last_success` only, so this daily step exported every day from
    # 2026-09-04 while the firing monitor read it as silent since its one consult trial. Guarded: a
    # ledger fault must not stop the export a remote consumer reads.
    try:
        capabilities.daily_heartbeat(
            "route-weights-export", "invocation", ref="route_weights_export.main"
        )
    except Exception as exc:  # noqa: BLE001 — a swallowed heartbeat reads as dormancy later
        print(f"route_weights_export: capability heartbeat failed: {exc}", file=sys.stderr)
    document = build_document(db_path, args.min_observations)
    print(
        f"route_weights_export: db={db_path} source_version={document['source_version']}", flush=True
    )
    target = args.state_dir / "route-weights-export.json"
    changed = write_document(target, document)
    print(f"{'wrote' if changed else 'unchanged'} {target}")
    if args.publish:
        if os.environ.get("ORCH_ROUTE_WEIGHTS_PUBLISH") != "1":
            print("publish blocked: ORCH_ROUTE_WEIGHTS_PUBLISH!=1; local export retained")
        else:
            try:
                published = publish_document(
                    document, allow_evidence_regression=args.allow_evidence_regression
                )
            except ValueError as exc:
                print(f"route_weights_export: {exc}", file=sys.stderr)
                return 1
            if published:
                try:
                    capabilities.daily_heartbeat(
                        "route-weights-export",
                        "success",
                        ref=EXPORT_BRANCH,
                        metadata={"source_version": document["source_version"], "published": True},
                    )
                except Exception as exc:  # noqa: BLE001 — telemetry cannot undo publication
                    print(
                        f"route_weights_export: capability heartbeat failed: {exc}", file=sys.stderr
                    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
