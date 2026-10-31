#!/usr/bin/env python3
"""Export conservative route-weight rankings for a fail-open remote consumer.

The learner remains local.  This module makes a versioned, thresholded snapshot
available for a consumer that deliberately falls back to its own static policy
whenever the export is missing, malformed, stale, or insufficiently evidenced.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import capabilities
import capacity
import comparative
import exploration_collection
import feedback
import provision
import router

# Workflows agent_delegation_policy.js accepts only this string; bumping to /v2
# would fail-open away from learned weights. Capacity is additive via CAPACITY_SCHEMA.
SCHEMA = "orchestrator.route-weights/v1"
CAPACITY_SCHEMA = "orchestrator.route-weights-capacity/v1"
DEFAULT_MIN_OBSERVATIONS = comparative.MIN_OBS
EXPORT_BRANCH = "exports/route-weights"
EXPORT_PATH = Path("config/route-weights.json")
CAPACITY_TICK_FILE = "route-weights-capacity-ticks.json"
CAPACITY_TICK_MAX_AGE = 2 * 3600
# Semantic mismatch alert allowance; independent of local tick-latch validity.
PUBLISH_LAG_STALE_S = 7200
PUBLISH_REFRESH_FLOOR_S = 24 * 3600
PERIOD_END_TOLERANCE_S = 3600
WEEK_SECONDS = 7 * 86400
# Owner q-1ca1d09d255e987b (2026-10-17): extend once if either policy is under-sampled.
EXPLORATION_WINDOW_END = "20261117"
EXPLORATION_WINDOW_EXTENDED_END = "20261215"
EXPLORATION_WINDOW_MIN_PER_POLICY = 30
EXPLORATION_WINDOW_START = "2026-10-17"
# Hosted repos lack GEMINI_API_KEY; Gemini remains available for local routing.
HOSTED_UNRUNNABLE_AGENTS = {"gemini"}
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


def update_exploration_window(
    db_path: Path, state_dir: Path, *, now: int | None = None, persist: bool = True
) -> dict:
    """Compute the shared read-only switch decision; ordinary exports also persist it."""
    timestamp = int(dt.datetime.now(dt.timezone.utc).timestamp()) if now is None else now
    instant = dt.datetime.fromtimestamp(timestamp, dt.timezone.utc)
    path = state_dir / "exploration-window.json"
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    counts = dict.fromkeys(("epsilon-greedy", "thompson-hybrid"), 0)
    errors = []
    start = int(
        dt.datetime.fromisoformat(EXPLORATION_WINDOW_START)
        .replace(tzinfo=dt.timezone.utc)
        .timestamp()
    )
    try:
        connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT switched_ts FROM agent_switches WHERE source='policy' "
                "AND delegation_source='route_weights_exploration' AND switched_ts>=? "
                "AND switched_ts<=?",
                (start, timestamp),
            )
            for (switched_ts,) in rows:
                # With an empty plan, choose_mode is deterministic in now (UTC-week parity).
                # _exploration_block uses this same call to choose the published policy.
                counts[exploration_collection.choose_mode({}, now=switched_ts)] += 1
        finally:
            connection.close()
    except sqlite3.Error as exc:
        errors.append(f"per-policy counts unmeasured: {exc}")
    extended = isinstance(previous, dict) and previous.get("extended") is True
    if instant.strftime("%Y%m%d") >= EXPLORATION_WINDOW_END and not errors:
        extended = extended or any(n < EXPLORATION_WINDOW_MIN_PER_POLICY for n in counts.values())
    window = {
        "start": EXPLORATION_WINDOW_START,
        "end": EXPLORATION_WINDOW_EXTENDED_END if extended else EXPLORATION_WINDOW_END,
        "extended": extended,
        "n_by_policy": counts if not errors else dict.fromkeys(counts, None),
        "min_per_policy": EXPLORATION_WINDOW_MIN_PER_POLICY,
        "decided_at": instant.isoformat().replace("+00:00", "Z"),
    }
    if errors:
        window["errors"] = errors
    if persist:
        write_document(path, window)
    return window


def publish_guard(
    new_doc: dict[str, Any], published_doc: dict[str, Any] | None
) -> tuple[bool, str]:
    """Refuse evidence loss independently of semantic change detection."""
    if (
        not isinstance(new_doc.get("source_version"), int)
        or isinstance(new_doc.get("source_version"), bool)
        or new_doc["source_version"] <= 0
    ):
        return (
            False,
            "source_version == 0 or invalid: local route-weight evidence is missing or empty",
        )
    tasks = new_doc.get("task_types", {})
    if not any(row.get("evidence_ok") is True for row in tasks.values()):
        return (
            False,
            "all task types have evidence_ok false; empty rankings would erase published evidence",
        )
    previous = (published_doc or {}).get("task_types", {})
    lost = sorted(
        name
        for name, row in previous.items()
        if row.get("evidence_ok") is True and tasks.get(name, {}).get("evidence_ok") is not True
    )
    if lost:
        return False, f"evidence_ok true -> false for: {', '.join(lost)}"
    return True, ""


class PublicationRefused(ValueError):
    """A proposed publication would erase learned routing evidence."""


def _regressing_tasks(document: dict[str, Any], published: dict[str, Any] | None) -> list[str]:
    tasks = document.get("task_types", {})
    return sorted(
        name
        for name, row in (published or {}).get("task_types", {}).items()
        if row.get("evidence_ok") is True and tasks.get(name, {}).get("evidence_ok") is not True
    )


def _require_publishable(
    document: dict[str, Any],
    published: dict[str, Any] | None,
    allow_evidence_regression: bool = False,
    *,
    state_dir: Path | None = None,
) -> None:
    ok, reason = publish_guard(document, published)
    lost = _regressing_tasks(document, published)
    # Pure validation must never read, reset, or create the live publication latch.
    hold_path = state_dir / "route-weights-regression-hold.json" if state_dir is not None else None
    if published is not None and hold_path is not None:
        if lost:
            now = dt.datetime.now(dt.timezone.utc).timestamp()
            try:
                hold = json.loads(hold_path.read_text())
                first_seen = float(hold["first_seen"])
                if hold["task_types"] != lost or not 0 <= first_seen <= now:
                    first_seen = now
            except (OSError, ValueError, KeyError, TypeError):
                first_seen = now
            write_document(
                hold_path, {"first_seen": first_seen, "task_types": lost, "reason": reason}
            )
        else:
            hold_path.unlink(missing_ok=True)
    if ok:
        return
    # Missing DB and wholly unevidenced output are absolute, including with override.
    absolute = not publish_guard(document, None)[0]
    if not absolute and allow_evidence_regression:
        print(f"publish_guard override --allow-evidence-regression: {reason}", file=sys.stderr)
        if hold_path is not None:
            hold_path.unlink(missing_ok=True)
        return
    override = "" if absolute else "; explicit --allow-evidence-regression required"
    raise PublicationRefused(
        f"publish_guard refused: {reason}; refused: regressing_tasks={len(lost)}, "
        f"drainable_in=infiniteh{override}"
    )


def _validate_publication(document: dict[str, Any], existing: dict[str, Any] | None = None) -> None:
    """Compatibility entry point; all callers share the absolute evidence rules."""
    _require_publishable(document, existing)


def _read_rows(db_path: Path) -> tuple[int, list[sqlite3.Row]]:
    """Return the latest legacy route-weight version without creating/migrating a DB."""
    if not db_path.exists():
        return 0, []
    connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
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


def _capacity_block(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Additive per-agent state/window/pacing contract for remote consumers."""
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
            **(
                {
                    "pacing": {
                        **row["pacing"],
                        "burn_per_day": row["pacing"].get("burn_rate_per_day"),
                    }
                }
                if isinstance(row.get("pacing"), dict)
                else {}
            ),
        }
    return block


def _read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _seen_count(tick: dict) -> int:
    try:
        return max(0, min(2, int(tick.get("seen", 0))))
    except (TypeError, ValueError, OverflowError):
        return 1


def _artifact_age(document: dict, now: int) -> int | None:
    try:
        generated = dt.datetime.fromisoformat(document["generated_at"].replace("Z", "+00:00"))
        return max(0, int(now - generated.timestamp()))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def _age_text(age: int | None) -> str:
    return "unmeasured" if age is None else f"{age}s"


def record_capacity_tick(state_dir: Path, *, now: int | None = None) -> bool:
    """Observe the tick-written local snapshot once, independently of publication retries.

    A changed state or a missed (>2h) observation resets the count to one. Reading
    the same snapshot again cannot drain the latch. The second distinct tick
    observation of that state is the only automatic drain, in either direction.
    """
    current = int(dt.datetime.now(dt.timezone.utc).timestamp()) if now is None else now
    snapshot = _read_object(capacity.OUT)
    observed = snapshot.get("generated_at")
    valid = isinstance(observed, (int, float)) and 0 <= current - observed <= CAPACITY_TICK_MAX_AGE
    previous = _read_object(state_dir / CAPACITY_TICK_FILE)
    ticks = {}
    if valid:
        for pool, row in _capacity_block(snapshot).items():
            old = previous.get(pool, {})
            seen = 1
            if (
                isinstance(old, dict)
                and old.get("state") == row["state"]
                and isinstance(old.get("observed_at"), (int, float))
                and 0 <= observed - old["observed_at"] <= CAPACITY_TICK_MAX_AGE
            ):
                seen = _seen_count(old)
                if observed > old["observed_at"]:
                    seen = min(2, seen + 1)
            ticks[pool] = {"state": row["state"], "observed_at": observed, "seen": seen}
    # A missing/stale local snapshot invalidates the previous observation.
    write_document(state_dir / CAPACITY_TICK_FILE, ticks)
    return bool(valid)


def damp_capacity_states(
    document: dict, published: dict | None, state_dir: Path, *, now=None
) -> dict:
    """Retain the published row until its proposed state has two local tick observations."""
    current = int(dt.datetime.now(dt.timezone.utc).timestamp()) if now is None else now
    ticks = _read_object(state_dir / CAPACITY_TICK_FILE)
    result = copy.deepcopy(document)
    baseline = (published or {}).get("capacity")
    baseline = baseline if isinstance(baseline, dict) else {}
    for pool, row in document.get("capacity", {}).items():
        old = baseline.get(pool)
        # Establish/repair an absent or malformed baseline before damping later state changes.
        if not isinstance(old, dict):
            continue
        if row.get("state") == old.get("state"):
            continue
        tick = ticks.get(pool, {})
        seen = 0
        if (
            isinstance(tick, dict)
            and tick.get("state") == row.get("state")
            and isinstance(tick.get("observed_at"), (int, float))
            and 0 <= current - tick["observed_at"] <= CAPACITY_TICK_MAX_AGE
        ):
            seen = _seen_count(tick)
        if seen < 2:
            print(
                f"[cadence] capacity publish held: {pool} local={row.get('state')} "
                f"published={old.get('state', 'unmeasured')} seen={seen}/2"
            )
            result["capacity"][pool] = copy.deepcopy(old)
    return result


def _fetch_export(checkout: Path) -> None:
    subprocess.run(
        ["git", "-C", str(checkout), "fetch", "--prune", "origin"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )


def read_published_export(*, remote_url="https://github.com/stranske/Orchestrator.git") -> dict:
    """Fetch the remote publication into disposable storage; no persistent refs/state writes."""
    with tempfile.TemporaryDirectory(prefix="route-weights-read-") as directory:
        checkout = Path(directory)
        subprocess.run(["git", "init", "--bare", str(checkout)], check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(checkout),
                "remote",
                "add",
                "-t",
                EXPORT_BRANCH,
                "origin",
                remote_url,
            ],
            check=True,
            capture_output=True,
        )
        _fetch_export(checkout)
        artifact = subprocess.run(
            ["git", "-C", str(checkout), "show", f"origin/{EXPORT_BRANCH}:{EXPORT_PATH}"],
            check=True,
            capture_output=True,
            text=True,
        )
        value = json.loads(artifact.stdout)
        if not isinstance(value, dict) or not isinstance(value.get("capacity"), dict):
            raise ValueError("unmeasured publication")
        return value


def publish_lag(*, now: int | None = None, state_dir: Path | None = None) -> str:
    """Read-only state comparison. Artifact age alone is not semantic lag."""
    current = int(dt.datetime.now(dt.timezone.utc).timestamp()) if now is None else now
    local = _read_object(capacity.OUT)
    try:
        published = read_published_export()
    except (OSError, ValueError, subprocess.SubprocessError):
        published = {}
    local_time = local.get("generated_at")
    local_age = max(0, int(current - local_time)) if isinstance(local_time, (int, float)) else None
    print(f"local capacity age={_age_text(local_age)}")
    ticks = _read_object(
        (state_dir or Path.home() / ".codex" / "orchestrator") / CAPACITY_TICK_FILE
    )
    published_age = _artifact_age(published, current)
    block = _capacity_block(local)
    in_sync = bool(published) and bool(local)
    lags = []
    for pool, row in block.items():
        remote = published.get("capacity", {}).get(pool, {})
        state = remote.get("state", "unmeasured") if isinstance(remote, dict) else "unmeasured"
        observed = remote.get("observed_at") if isinstance(remote, dict) else None
        age = (
            max(0, int(current - observed)) if isinstance(observed, (int, float)) else "unmeasured"
        )
        matches = bool(local) and row["state"] == state
        in_sync = in_sync and matches
        tick = ticks.get(pool, {})
        held = (
            not matches
            and state != "unmeasured"
            and isinstance(tick, dict)
            and tick.get("state") == row["state"]
            and isinstance(tick.get("observed_at"), (int, float))
            and 0 <= current - tick["observed_at"] <= CAPACITY_TICK_MAX_AGE
            and _seen_count(tick) < 2
        )
        lag = (
            f"held({_seen_count(tick)}/2)"
            if held
            else (
                "ok"
                if matches
                or (
                    state != "unmeasured"
                    and published_age is not None
                    and published_age <= PUBLISH_LAG_STALE_S
                )
                else "STALE"
            )
        )
        lags.append(lag)
        print(
            f"pool {pool}: local={row['state']} published={state} "
            f"published_age={_age_text(published_age)} lag={lag} "
            f"observed_age={_age_text(age if isinstance(age, int) else None)}"
        )
    if in_sync:
        print("all pools in sync")
    if not local or not published or not lags:
        return "unmeasured"
    if "STALE" in lags:
        return "STALE"
    return next((lag for lag in lags if lag.startswith("held(")), "ok")


def _fetch_publish_commits(endpoint: str) -> list[dict[str, Any]]:
    """Read all commit pages; gh's default GET performs no remote mutation."""
    response = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", endpoint],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    pages = json.loads(response.stdout)
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise ValueError("unmeasured commit history")
    commits = [commit for page in pages for commit in page]
    if any(not isinstance(commit, dict) or not commit.get("sha") for commit in commits):
        raise ValueError("unmeasured commit history")
    return commits


def publish_history(
    *,
    now: int | None = None,
    state_dir: Path | None = None,
    fetcher: Callable[[str], list[dict[str, Any]]] | None = None,
) -> None:
    """Report the rolling 24h count and semantic lag without publishing or updating local state."""
    current = int(dt.datetime.now(dt.timezone.utc).timestamp()) if now is None else now
    since = (
        dt.datetime.fromtimestamp(current - 86400, dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    endpoint = f"repos/stranske/Orchestrator/commits?sha={EXPORT_BRANCH}&since={since}&per_page=100"
    count: int | str
    try:
        count = len((fetcher or _fetch_publish_commits)(endpoint))
    except (OSError, ValueError, subprocess.SubprocessError):
        count = "unmeasured"
    lag = publish_lag(now=current, state_dir=state_dir)
    print(f"commits_24h={count} (state flips local: see log) lag={lag}")


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
    eligible = (
        router.KEEPALIVE_AGENTS
        - router.RESERVE_AGENTS
        - router.BACKUP_AGENTS
        - HOSTED_UNRUNNABLE_AGENTS
    )
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
    try:
        float(os.environ["ORCH_EXPLORATION_RATE"])
        rate_source = "env"
    except (KeyError, ValueError):
        rate_source = "default"
    return {
        "mode": mode,
        "rate": router._exploration_rate(None),
        "rate_source": rate_source,
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
    # Emit provenance before capacity/exploration work: a later failure must not
    # hide which Brain supplied this run's evidence.
    print(f"db={db_path} source_version={version}", file=sys.stderr, flush=True)
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
    comparison_now = timestamp
    if include_capacity:
        if capacity_snapshot is None:
            capacity_snapshot = _current_capacity_snapshot()
            # The policy/window clock precedes live collection. Measure the fresh
            # sample against the clock after collection, never its own claimed ts.
            comparison_now = int(
                dt.datetime.fromisoformat(_now().replace("Z", "+00:00")).timestamp()
            )
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
    _add_comparative(document, db_path, capacity_snapshot, comparison_now)
    return document


def _add_comparative(document: dict, db_path: Path, snapshot: dict | None, now: int) -> None:
    """Add shadow ranks after hashing the existing policy; never reorder its entries."""
    # Opportunity cost includes every evidenced class, beyond this export's consumer set.
    result = comparative.measure(db_path, snapshot, now=now, minimum=document["min_observations"])
    for task, section in document["task_types"].items():
        _attach_comparative_ranks(section["ranking"], result, task, document["min_observations"])
    for task, rows in document["reserve"].items():
        _attach_comparative_ranks(rows, result, task, document["min_observations"])
    document["comparative"] = {key: value for key, value in result.items() if key != "ranks"}


def _canonical_bytes(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _attach_comparative_ranks(
    rows: list[dict[str, Any]], result: dict, task: str, minimum: int = DEFAULT_MIN_OBSERVATIONS
) -> None:
    """Global comparative ranks (one enumeration per task, reserve included) plus a reason when absent."""
    status = result["status"]
    status_text = status + (": " + "; ".join(result["reasons"]) if result["reasons"] else "")
    positions = {
        row["agent"]: index
        for index, row in enumerate(result["ranks"].get(task, []), 1)
        if row["comparative_score"] is not None
    }
    for row in rows:
        row["comparative_rank"] = positions.get(row["agent"]) if status == "optimal" else None
        row["comparative_status"] = status_text
        if row["comparative_rank"] is not None:
            continue
        if row["n_obs"] < minimum:
            # Keep the diagnostic tied to the same threshold used for this export.
            row["comparative_reason"] = f"n_obs below comparative threshold ({minimum})"
        else:
            row["comparative_reason"] = result["inputs"]["cost_source_by_agent"].get(
                row["agent"], {}
            ).get("reason") or "; ".join(result["reasons"])


def _semantic_for_compare(document: dict[str, Any]) -> dict[str, Any]:
    """Keep actionable routing/capacity content; comparative fields are shadow diagnostics.

    Keep malformed structures visible for repair. Other diagnostics ship on a
    meaningful trigger or the refresh floor. Period-end tolerance is pairwise
    against the published baseline in _same_semantics, never a clock bucket.
    """
    semantic = {
        key: value for key, value in document.items() if key not in ("generated_at", "comparative")
    }
    capacity_block = semantic.get("capacity")
    if isinstance(capacity_block, dict):
        compared: dict[str, Any] = {}
        for agent, entry in capacity_block.items():
            if isinstance(entry, dict):
                compared[agent] = {"state": entry.get("state")}
                pace = entry.get("pacing", {})
                if isinstance(pace, dict):
                    compared[agent]["pacing"] = {
                        key: value
                        for key, value in pace.items()
                        if key in ("period_end", "resets_available")
                    }
                elif "pacing" in entry:
                    compared[agent]["pacing"] = pace
            else:
                compared[agent] = entry
        semantic["capacity"] = compared

    for key in ("task_types", "reserve"):
        if key in semantic:
            semantic[key] = _without_comparative_rows(semantic[key], rankings=key == "task_types")
    return semantic


def _without_comparative_rows(section, *, rankings):
    if not isinstance(section, dict):
        return section

    def without_comparative(row):
        return (
            {key: value for key, value in row.items() if not key.startswith("comparative_")}
            if isinstance(row, dict)
            else row
        )

    result = {}
    for task, entry in section.items():
        if rankings and isinstance(entry, dict):
            entry = dict(entry)
            if isinstance(entry.get("ranking"), list):
                entry["ranking"] = [without_comparative(row) for row in entry["ranking"]]
        elif not rankings and isinstance(entry, list):
            entry = [without_comparative(row) for row in entry]
        result[task] = entry
    return result


def _same_semantics(document: dict, published: dict) -> bool:
    proposed = _semantic_for_compare(document)
    baseline = _semantic_for_compare(published)
    incoming = proposed.get("capacity")
    previous = baseline.get("capacity")
    if isinstance(incoming, dict) and isinstance(previous, dict):
        for pool, row in incoming.items():
            old = previous.get(pool)
            if not isinstance(row, dict) or not isinstance(old, dict):
                continue
            pace, old_pace = row.get("pacing"), old.get("pacing")
            if not isinstance(pace, dict) or not isinstance(old_pace, dict):
                continue
            end, old_end = pace.get("period_end"), old_pace.get("period_end")
            if (
                isinstance(end, (int, float))
                and not isinstance(end, bool)
                and isinstance(old_end, (int, float))
                and not isinstance(old_end, bool)
                and abs(end - old_end) <= PERIOD_END_TOLERANCE_S
            ):
                pace["period_end"] = old_end
    return proposed == baseline


def write_document(path: Path, document: dict[str, Any], *, refresh: bool = False) -> bool:
    """Write only semantic changes, retaining the earlier timestamp on an unchanged export."""
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            existing = None
        if isinstance(existing, dict):
            if not refresh and _same_semantics(document, existing):
                return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(_canonical_bytes(document))
    temporary.replace(path)
    return True


def publish_document(
    document: dict[str, Any],
    *,
    allow_evidence_regression: bool = False,
    state_dir: Path | None = None,
    db_path: Path | None = None,
) -> bool:
    """Commit and push the export branch only when its remote artifact changes."""
    # Reject an empty source even before provisioning/fetching a publication checkout.
    _require_publishable(document, None, allow_evidence_regression)
    if allow_evidence_regression:
        print("publish_guard: --allow-evidence-regression requested", file=sys.stderr)

    canonical = provision.ensure_canonical("stranske/Orchestrator")
    _fetch_export(canonical)
    published = None
    remote_ref = f"origin/{EXPORT_BRANCH}"
    has_remote = (
        subprocess.run(
            ["git", "-C", str(canonical), "rev-parse", "--verify", "--quiet", remote_ref],
            check=False,
        ).returncode
        == 0
    )
    if has_remote:
        # Compare the fetched publication, never the local shadow export (which
        # this run may already have replaced). Read before switching or writing.
        artifact = subprocess.run(
            ["git", "-C", str(canonical), "show", f"{remote_ref}:{EXPORT_PATH}"],
            check=False,
            capture_output=True,
            text=True,
        )
        if artifact.returncode:
            # Only an absent path is recoverable; other Git read errors stay fatal.
            listing = subprocess.run(
                [
                    "git",
                    "-C",
                    str(canonical),
                    "ls-tree",
                    "--name-only",
                    remote_ref,
                    "--",
                    str(EXPORT_PATH),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            if listing.stdout.strip():
                artifact.check_returncode()
            print(
                "refused: published artifact missing on branch; publishing fresh", file=sys.stderr
            )
            published = None
        else:
            try:
                loaded = json.loads(artifact.stdout)
            except json.JSONDecodeError as exc:
                raise PublicationRefused(
                    f"publish_guard refused: remote {EXPORT_PATH} is not valid JSON ({exc})"
                ) from exc
            if not isinstance(loaded, dict):
                raise PublicationRefused(
                    f"publish_guard refused: remote {EXPORT_PATH} is not a JSON object"
                )
            remote_tasks = loaded.get("task_types", {})
            if not isinstance(remote_tasks, dict) or not all(
                isinstance(row, dict) for row in remote_tasks.values()
            ):
                raise PublicationRefused(
                    f"publish_guard refused: remote {EXPORT_PATH} has malformed task_types"
                )
            published = loaded
        _require_publishable(document, published, allow_evidence_regression, state_dir=state_dir)
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
    document = damp_capacity_states(
        document, published, state_dir or Path.home() / ".codex" / "orchestrator"
    )
    # A diagnostic-only refresh is allowed only after the published artifact ages 24h.
    now = int(dt.datetime.fromisoformat(_now().replace("Z", "+00:00")).timestamp())
    published_age = _artifact_age(published or {}, now)
    semantic_equal = published is not None and _same_semantics(document, published)
    refresh = semantic_equal and (published_age is None or published_age > PUBLISH_REFRESH_FLOOR_S)
    # Compare the fetched remote artifact before touching a potentially divergent local shadow.
    if semantic_equal and not refresh:
        print(
            f"unchanged published_age={_age_text(published_age)}/floor={PUBLISH_REFRESH_FLOOR_S}s"
        )
        return False
    if refresh:
        print(f"refresh published_age={_age_text(published_age)}/floor={PUBLISH_REFRESH_FLOOR_S}s")
        document = {**document, "generated_at": _now()}
    target = canonical / EXPORT_PATH
    if not write_document(target, document, refresh=refresh):
        print(
            f"unchanged published_age={_age_text(published_age)}/floor={PUBLISH_REFRESH_FLOOR_S}s"
        )
        return False
    subprocess.run(["git", "-C", str(canonical), "add", str(EXPORT_PATH)], check=True)
    identity = (
        subprocess.run(
            ["git", "-C", str(canonical), "config", "user.name"],
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
        or "orchestrator-export"
    )
    resolved_db = feedback.resolve_db_path(db_path)
    message = (
        f"chore: export route weights (db={resolved_db} "
        f"source_version={document['source_version']} by={identity})"
    )
    subprocess.run(["git", "-C", str(canonical), "commit", "-m", message], check=True)
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
        import comparative

        assert document["comparative"]["schema"] == comparative.SCHEMA, document["comparative"]
        assert all(
            "comparative_rank" in row
            for section in document["task_types"].values()
            for row in section["ranking"]
        ), document
        output = root / "route-weights-export.json"
        assert write_document(output, document) is True
        # A changed comparative_reason alone is narration, never a republish.
        reason_only = json.loads(json.dumps(document))
        for row in reason_only["task_types"]["implement"]["ranking"]:
            row["comparative_reason"] = "reworded"
        assert write_document(output, reason_only) is False
        # The report-only CLI path reads, prints and writes nothing.
        before = sorted(p.name for p in root.iterdir())
        text = comparative.format_report(comparative.report(db_path, root / "no-ledger.ndjson"))
        assert "classes_differing" in text, text
        assert sorted(p.name for p in root.iterdir()) == before
        assert (
            write_document(
                output,
                build_document(
                    db_path, minimum=20, now=fixed_now, capacity_snapshot=capacity_snapshot
                ),
            )
            is False
        )
        # State triggers publication; window and observed_at are carried on other triggers.
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
            is False
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
        # Publication must reject a missing evidence DB even when capacity is fresh.
        empty = build_document(root / "missing.db", capacity_snapshot=capacity_snapshot)
        try:
            _validate_publication(empty)
        except ValueError:
            pass
        else:
            raise AssertionError("missing evidence must not publish")
        _require_publishable(document, empty, state_dir=root)
        # A partial regression never ages into permission; only explicit override releases it.
        previous = json.loads(json.dumps(document))
        previous["task_types"]["review"]["evidence_ok"] = True
        hold_path = root / "route-weights-regression-hold.json"
        try:
            _require_publishable(document, previous, state_dir=root)
        except PublicationRefused as exc:
            assert "regressing_tasks=1, drainable_in=infiniteh" in str(exc), exc
        else:
            raise AssertionError("fresh regression must hold")
        hold = json.loads(hold_path.read_text())
        first_seen = hold["first_seen"]
        try:
            _require_publishable(document, previous, state_dir=root)
        except PublicationRefused:
            pass
        else:
            raise AssertionError("repeated regression must still hold")
        assert json.loads(hold_path.read_text())["first_seen"] == first_seen
        hold["first_seen"] -= 240 * 3600
        write_document(hold_path, hold)
        try:
            _require_publishable(document, previous, state_dir=root)
        except PublicationRefused as exc:
            assert "--allow-evidence-regression required" in str(exc), exc
        else:
            raise AssertionError("aged partial regression requires explicit override")
        assert json.loads(hold_path.read_text())["first_seen"] == hold["first_seen"]
        _require_publishable(previous, previous, state_dir=root)
        assert not hold_path.exists(), "evidence recovery must clear the hold"
        # Override bypasses only a partial regression; absolute refusals stay refused.
        try:
            _require_publishable(document, previous, state_dir=root)
        except PublicationRefused:
            pass
        else:
            raise AssertionError("fresh partial regression must hold without override")
        _require_publishable(document, previous, allow_evidence_regression=True, state_dir=root)
        assert not hold_path.exists(), "explicit override clears the hold"
        try:
            _require_publishable(empty, previous, allow_evidence_regression=True, state_dir=root)
        except PublicationRefused as exc:
            assert "drainable_in=infiniteh" in str(exc), exc
        else:
            raise AssertionError("override must not publish empty evidence")
        all_false = json.loads(json.dumps(document))
        all_false["source_version"] = 1
        for row in all_false["task_types"].values():
            row["evidence_ok"] = False
            row["ranking"] = []
        try:
            _require_publishable(
                all_false, previous, allow_evidence_regression=True, state_dir=root
            )
        except PublicationRefused as exc:
            assert "drainable_in=infiniteh" in str(exc), exc
        else:
            raise AssertionError("override must not publish all-false evidence")
    print("route_weights_export.py selftest: OK")


def main() -> int:
    import comparative

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-observations", type=int, default=DEFAULT_MIN_OBSERVATIONS)
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.environ["ORCH_STATE_DIR"]) if os.environ.get("ORCH_STATE_DIR") else None,
    )
    parser.add_argument("--feedback-db", type=Path)
    parser.add_argument("--as-of", type=int, help="UTC decision instant shared with this tick")
    parser.add_argument(
        "--preview-exploration-window",
        action="store_true",
        help="read-only preview of the exploration decision; print end and UTC day",
    )
    parser.add_argument(
        "--refresh-exploration-window",
        action="store_true",
        help="persist only the exploration decision; print end and UTC day",
    )
    parser.add_argument("--comparative-report", action="store_true")
    parser.add_argument(
        "--publish-lag", action="store_true", help="read-only local/published pool states"
    )
    parser.add_argument(
        "--publish-history", choices=["24h"], help="read-only rolling publication count and lag"
    )
    parser.add_argument(
        "--record-capacity-tick", action="store_true", help="observe local state once per tick"
    )
    parser.add_argument("--window-days", type=int, default=comparative.WINDOW_DAYS)
    parser.add_argument(
        "--pacing-history",
        type=Path,
        help="capacity-ledger.ndjson pacing_snapshot rows (or legacy pacing-history.ndjson)",
    )
    parser.add_argument(
        "--allow-evidence-regression",
        action="store_true",
        help="explicit owner override for an evidence downgrade (logged)",
    )
    parser.add_argument(
        "--publish", action="store_true", help="publish only with ORCH_ROUTE_WEIGHTS_PUBLISH=1"
    )
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        _selftest()
        return 0
    if args.publish_history:
        publish_history(state_dir=args.state_dir)
        return 0
    if args.publish_lag:
        publish_lag(state_dir=args.state_dir)
        return 0
    if args.record_capacity_tick:
        return (
            0
            if record_capacity_tick(args.state_dir or Path.home() / ".codex" / "orchestrator")
            else 1
        )
    db_path = feedback.resolve_db_path(args.feedback_db)
    if args.comparative_report:
        if args.window_days < 1:
            parser.error("--window-days must be positive")
        print(
            comparative.format_report(
                comparative.report(
                    db_path,
                    args.pacing_history or comparative.history_path(),
                    window_days=args.window_days,
                )
            )
        )
        return 0
    state_dir = args.state_dir or Path.home() / ".codex" / "orchestrator"
    timestamp = args.as_of
    if timestamp is None:
        timestamp = int(dt.datetime.fromisoformat(_now().replace("Z", "+00:00")).timestamp())
    window = update_exploration_window(
        db_path, state_dir, now=timestamp, persist=not args.preview_exploration_window
    )
    day = dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).strftime("%Y%m%d")
    if args.preview_exploration_window or args.refresh_exploration_window:
        print(window["end"], day)
        return 0
    # Match the tick's window default even for a standalone export. Never replace an
    # explicit caller setting, and do not leave a temporary default in the caller.
    previous_rate = os.environ.get("ORCH_EXPLORATION_RATE")
    if not previous_rate and day <= window["end"]:
        os.environ["ORCH_EXPLORATION_RATE"] = "0.20"
    try:
        document = build_document(db_path, args.min_observations, now=timestamp)
    finally:
        if previous_rate is None:
            os.environ.pop("ORCH_EXPLORATION_RATE", None)
        else:
            os.environ["ORCH_EXPLORATION_RATE"] = previous_rate
    # Credit INVOCATION only after the document builds; `success` below records publication's
    # result. A success alone moves `last_success` only, so the former daily step exported every day from
    # 2026-09-04 while the firing monitor read it as silent since its one consult trial. Guarded: a
    # ledger fault must not stop the export a remote consumer reads.
    try:
        capabilities.daily_heartbeat(
            "route-weights-export", "invocation", ref="route_weights_export.main"
        )
    except Exception as exc:  # noqa: BLE001 — a swallowed heartbeat reads as dormancy later
        print(f"route_weights_export: capability heartbeat failed: {exc}", file=sys.stderr)
    target = state_dir / "route-weights-export.json"
    changed = write_document(target, document)
    print(f"hosted-unrunnable agents excluded: {', '.join(sorted(HOSTED_UNRUNNABLE_AGENTS))}")
    print(f"{'wrote' if changed else 'unchanged'} {target}")
    if args.publish:
        if os.environ.get("ORCH_ROUTE_WEIGHTS_PUBLISH") != "1":
            print("publish blocked: ORCH_ROUTE_WEIGHTS_PUBLISH!=1; local export retained")
        else:
            try:
                published = publish_document(
                    document,
                    allow_evidence_regression=args.allow_evidence_regression,
                    state_dir=state_dir,
                    db_path=db_path,
                )
            except PublicationRefused as exc:
                print(str(exc), file=sys.stderr)
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
