#!/usr/bin/env python3
"""Fleet-native work shapes: what the fleet's merged PRs touched, grouped, and how each shape fared.

WHY THIS EXISTS (the 2026-09-15 evaluation, direction 5: "re-key pattern mining to fleet-native
shapes"). Pattern mining keyed on this tool's own completion events, which require a research-subject
identity — spec hash, base SHA, arm set, resolved model. The fleet's work never carries one: 5,748 of
5,770 events were excluded and 0 episodes were ever assembled, while some 540 merged agent PRs a month
went unmined. This module re-keys the mining to the population that exists. A merged fleet PR's SHAPE
is the commit type in its title, its label family (agent/status/priority labels dropped) and the path
classes it touched (workflows, github-meta, tests, docs, scripts, config, bookkeeping, code). Shapes
that recur across repos are the natural unit for codemod-campaign and for routing by work shape; per
shape and agent the module measures how many merges broke later, the hours from open to merge, the
cost the Brain recorded, and the PR's commit count as a stand-in for keepalive rounds, which the Brain
does not hold.

DEDUP (2026-09-17). Searched the tree for fleet_shape / pr_shape / change_shape (absent); the
improvement log for "fleet shape" (no item); pattern_miner.py (bound to the seven-phase envelope, see
above); capability_advisor._probe_repeated_pattern (keyword-only: codemod/mechanical/sweep in the
title). Not present. Built as a sibling cadence step to the miner — the miner keeps its contract and
its "no research subject" diagnosis — consumed by the advisor's `repeated_pattern` precondition and by
the periodic report.

INPUTS. The Brain: merged keepalive rows joined to outcomes and costs, agents only — bot and owner
rows carry attribution_source `bot:*` / `human` and are excluded, and `agent='none'` rows are the
unattributed remainder, also excluded. GitHub: ONE GraphQL read per 40 PRs for title, labels, files,
createdAt, mergedAt and commit count, cached in `<state>/fleet-shapes-facts.json` so a PR is fetched
once; a PR gh cannot return is recorded as unavailable and counted as missing, never as a shape.

OUTPUTS. `<state>/fleet-shapes.json` (machine) and `<state>/fleet-shapes.md` (human). This module
never creates a review queue, dispatches anything, or writes to the Brain. Kill switch:
`ORCH_DISABLE_STEPS=fleet-shapes` (the cadence registry's generic switch).

    python3 fleet_shapes.py run [--window-days 60] [--state-dir DIR] [--fetch-limit 400] [--json]
    python3 fleet_shapes.py show [--top 10] [--state-dir DIR]
    python3 fleet_shapes.py --selftest
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import feedback

SCHEMA = "orchestrator.fleet-shapes"
VERSION = 1
FACTS_SCHEMA = "orchestrator.fleet-shapes-facts"
DEFAULT_WINDOW_DAYS = 60
DURABILITY_MIN_AGE_DAYS = 7
FETCH_LIMIT_DEFAULT = 400
GRAPHQL_CHUNK = 40
# A shape RECURS when three or more merged PRs share it across two or more repos, or five in one repo.
RECURRING_MIN_PRS = 3
RECURRING_MIN_REPOS = 2
SINGLE_REPO_MIN_PRS = 5
MAX_PATH_CLASSES = 3
MAX_LABELS = 3
EXAMPLES_PER_SHAPE = 5
MISSING_REPORTED_MAX = 50
BAD_DURABILITY = ("broke_later", "reverted", "reopened", "abandoned", "reworked")
ROUNDS_PROXY = "PR commit count — the Brain holds no keepalive round count"
COMMIT_TYPE_RE = re.compile(
    r"^\s*(?:\[[^\]]*\]\s*)?(feat|fix|chore|docs?|tests?|refactor|ci|build|perf|style|revert)\b",
    re.IGNORECASE,
)
COMMIT_TYPE_ALIASES = {"doc": "docs", "test": "tests"}
# Labels that say who works a PR or where it sits in a queue, not what kind of work it is.
DROPPED_LABEL_PREFIXES = (
    "agent:",
    "agents:",
    "status:",
    "priority:",
    "delivery-",
    "size/",
    "autofix",
    "keepalive",
)
CONFIG_FILES = {
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "requirements-dev.txt",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "makefile",
    "dockerfile",
    "tox.ini",
    "renovate.json",
    "mypy.ini",
    "pytest.ini",
}
CONFIG_SUFFIXES = (".toml", ".cfg", ".ini", ".lock", ".yaml", ".yml", ".json")
PATH_CLASSES = (
    "workflows",
    "github-meta",
    "bookkeeping",
    "tests",
    "docs",
    "scripts",
    "config",
    "code",
)

_LOOKUP_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def default_state_dir() -> Path:
    return Path(os.environ.get("ORCH_STATE_DIR", Path.home() / ".codex" / "orchestrator"))


# ---------------------------------------------------------------- shape --------------------------


def path_class(path: str) -> str:
    """One of PATH_CLASSES for a repository path. Deterministic, extension- and directory-based."""
    p = str(path or "").strip()
    if p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/").lower()
    if not p:
        return "code"
    if p.startswith(".github/workflows/"):
        return "workflows"
    if p.startswith(".github/"):
        return "github-meta"
    if p.startswith(".agents/"):
        return "bookkeeping"
    parts = p.split("/")
    name = parts[-1]
    dirs = parts[:-1]
    if (
        "tests" in dirs
        or "test" in dirs
        or name.startswith("test_")
        or name.endswith(("_test.py", ".test.ts", ".test.tsx", ".spec.ts", ".spec.js"))
    ):
        return "tests"
    if parts[0] in ("docs", "doc") or name.endswith((".md", ".rst")):
        return "docs"
    if parts[0] in ("scripts", "bin"):
        return "scripts"
    if len(parts) == 1 and (
        name in CONFIG_FILES or name.endswith(CONFIG_SUFFIXES) or name.startswith(".")
    ):
        return "config"
    return "code"


def label_family(labels: list[str] | None) -> list[str]:
    kept = {
        str(lab).strip().lower()
        for lab in labels or []
        if str(lab).strip() and not str(lab).strip().lower().startswith(DROPPED_LABEL_PREFIXES)
    }
    return sorted(kept)[:MAX_LABELS]


def commit_type(title: str | None) -> str:
    match = COMMIT_TYPE_RE.match(title or "")
    if not match:
        return "other"
    kind = match.group(1).lower()
    return COMMIT_TYPE_ALIASES.get(kind, kind)


def shape_signature(
    title: str | None, labels: list[str] | None, paths: list[str] | None
) -> dict[str, Any]:
    """The shape of one PR: commit type | label family | path classes (the most-touched, ≤3, sorted)."""
    counts: dict[str, int] = {}
    for path in paths or []:
        if path:
            cls = path_class(str(path))
            counts[cls] = counts.get(cls, 0) + 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_PATH_CLASSES]
    classes = sorted(cls for cls, _ in top)
    kind = commit_type(title)
    family = label_family(labels)
    return {
        "key": f"{kind}|{','.join(family)}|{'+'.join(classes)}",
        "commit_type": kind,
        "labels": family,
        "path_classes": classes,
    }


# ---------------------------------------------------------------- brain --------------------------


def merged_agent_prs(
    *, window_days: int = DEFAULT_WINDOW_DAYS, now: int | None = None
) -> list[dict]:
    """Merged keepalive rows for agents, inside the window. Bot and owner rows are excluded by their
    attribution_source; unattributed (`agent='none'`) rows are excluded too — a shape with no agent
    cannot be compared by agent, and the ingest resolves them on its own schedule."""
    now = int(time.time()) if now is None else int(now)
    since = now - window_days * 86400
    out: list[dict] = []
    with feedback._conn() as c:
        rows = c.execute(
            "SELECT r.run_id, r.target, r.agent, r.ts, r.routing_metadata, o.durability, "
            "o.durability_checked_ts, "
            "o.verifier_verdict, (SELECT SUM(k.cost_usd) FROM costs k WHERE k.run_id=r.run_id) "
            "FROM runs r JOIN outcomes o ON o.run_id=r.run_id "
            "WHERE o.merged=1 AND r.source='keepalive' AND r.ts>=? AND r.agent NOT IN ('none','') "
            "ORDER BY r.ts",
            (since,),
        ).fetchall()
    for _run_id, target, agent, ts, metadata_raw, durability, checked_ts, verifier, cost in rows:
        source = str(feedback._routing_metadata_dict(metadata_raw).get("attribution_source") or "")
        if source.startswith("bot:") or source == "human":
            continue
        repo, _, number = str(target or "").partition("#")
        if not repo or not number.isdigit():
            continue
        judged_ts = checked_ts if checked_ts is not None else ts
        # Keep the PR in shape and merge-time counts. A pre-detection or young merged PR has
        # no trustworthy broke-later observation yet, even if its stored label says durable.
        durability_ready = (
            judged_ts >= feedback.DURABILITY_DETECTION_SINCE
            and ts <= now - DURABILITY_MIN_AGE_DAYS * 86400
        )
        out.append(
            {
                "ref": f"{repo}#{number}",
                "repo": repo,
                "number": int(number),
                "agent": str(agent),
                "ts": int(ts),
                "durability": str(durability or "pending") if durability_ready else "pending",
                "verifier": verifier,
                "cost_usd": float(cost) if cost is not None else None,
            }
        )
    return out


# ---------------------------------------------------------------- github facts -------------------


def _gh_json(args: list[str], *, timeout: int = 120) -> object | None:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


def _epoch(iso: object) -> int | None:
    if not isinstance(iso, str) or not iso:
        return None
    try:
        return int(time.mktime(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone)
    except ValueError:
        return None


def _fact_from_graphql(pr: dict[str, Any]) -> dict[str, Any]:
    labels_node = pr.get("labels") if isinstance(pr.get("labels"), dict) else {}
    labels = [
        str(n.get("name"))
        for n in (labels_node or {}).get("nodes") or []
        if isinstance(n, dict) and n.get("name")
    ]
    files_node = pr.get("files") if isinstance(pr.get("files"), dict) else {}
    file_rows = [n for n in (files_node or {}).get("nodes") or [] if isinstance(n, dict)]
    commits_node = pr.get("commits") if isinstance(pr.get("commits"), dict) else {}
    return {
        "title": str(pr.get("title") or ""),
        "labels": labels,
        "paths": [str(n.get("path")) for n in file_rows if n.get("path")],
        "files_total": (files_node or {}).get("totalCount"),
        "additions": sum(int(n.get("additions") or 0) for n in file_rows),
        "deletions": sum(int(n.get("deletions") or 0) for n in file_rows),
        "created_ts": _epoch(pr.get("createdAt")),
        "merged_ts": _epoch(pr.get("mergedAt")),
        "commits": (commits_node or {}).get("totalCount"),
    }


def fetch_facts(
    repo: str,
    numbers: list[int],
    *,
    gh_json: Callable[[list[str]], object | None] = _gh_json,
) -> dict[int, dict[str, Any]]:
    """One GraphQL query per 40 PRs. A chunk that fails as a whole (one deleted PR fails the query) is
    retried one PR at a time so the others keep their facts; a PR still missing is simply absent."""
    owner, _, name = repo.partition("/")
    out: dict[int, dict[str, Any]] = {}
    for i in range(0, len(numbers), GRAPHQL_CHUNK):
        chunk = numbers[i : i + GRAPHQL_CHUNK]
        fields = " ".join(
            f"p{n}: pullRequest(number:{n}){{ number title createdAt mergedAt "
            f"labels(first:30){{nodes{{name}}}} "
            f"files(first:100){{totalCount nodes{{path additions deletions}}}} "
            f"commits{{totalCount}} }}"
            for n in chunk
        )
        query = f'query {{ repository(owner:"{owner}", name:"{name}") {{ {fields} }} }}'
        data = gh_json(["gh", "api", "graphql", "-f", f"query={query}"])
        payload = data.get("data") if isinstance(data, dict) else None
        repo_data = payload.get("repository") if isinstance(payload, dict) else None
        if not isinstance(repo_data, dict):
            if len(chunk) > 1:
                for n in chunk:
                    out.update(fetch_facts(repo, [n], gh_json=gh_json))
            continue
        for pr in repo_data.values():
            if isinstance(pr, dict) and pr.get("number") is not None:
                out[int(pr["number"])] = _fact_from_graphql(pr)
    return out


def load_facts(state_dir: Path) -> dict[str, dict[str, Any]]:
    path = state_dir / "fleet-shapes-facts.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    facts = payload.get("facts") if isinstance(payload, dict) else None
    return {str(k): v for k, v in (facts or {}).items() if isinstance(v, dict)}


def time_to_merge_summary(
    state_dir: Path,
    window_days: int,
    *,
    now: int | None = None,
    facts: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Summarize merged agent PRs using the cached GitHub open and merge times."""
    if facts is None and not (state_dir / "fleet-shapes-facts.json").is_file():
        return {
            "value": None,
            "unmeasured": "the Brain records no merge timestamp (runs.ts is the PR's creation/ingest time, outcomes hold no mergedAt) and the shape facts cache is absent",
        }
    if facts is None:
        facts = load_facts(state_dir)
    cells: dict[str, dict[str, Any]] = {}
    seen: set[tuple[str, str]] = set()
    for pr in merged_agent_prs(window_days=window_days, now=now):
        agent, ref = pr["agent"], pr["ref"]
        if (agent, ref) in seen:
            continue
        seen.add((agent, ref))
        cell = cells.setdefault(agent, {"n_merged": 0, "hours": []})
        cell["n_merged"] += 1
        fact = facts.get(ref, {})
        created, merged = fact.get("created_ts"), fact.get("merged_ts")
        if (
            isinstance(created, (int, float))
            and not isinstance(created, bool)
            and isinstance(merged, (int, float))
            and not isinstance(merged, bool)
            and math.isfinite(created)
            and math.isfinite(merged)
            and merged >= created
        ):
            cell["hours"].append((merged - created) / 3600)
    summary: dict[str, Any] = {}
    for agent, cell in sorted(cells.items()):
        hours = cell["hours"]
        p90 = (
            statistics.quantiles(hours, n=10, method="inclusive")[8]
            if len(hours) > 1
            else hours[0] if hours else None
        )
        summary[agent] = {
            "median_hours": round(statistics.median(hours), 2) if hours else None,
            "p90_hours": round(p90, 2) if p90 is not None else None,
            "n_with_facts": len(hours),
            "n_merged": cell["n_merged"],
        }
    return summary


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=1, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def save_facts(state_dir: Path, facts: dict[str, dict[str, Any]], *, now: int) -> None:
    write_json_atomic(
        state_dir / "fleet-shapes-facts.json",
        {"schema": FACTS_SCHEMA, "version": VERSION, "updated_at": now, "facts": facts},
    )


# ---------------------------------------------------------------- aggregate ----------------------


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


def _new_shape(sig: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": sig["key"],
        "commit_type": sig["commit_type"],
        "labels": sig["labels"],
        "path_classes": sig["path_classes"],
        "prs": 0,
        "repos": set(),
        "agents": {},
        "examples": [],
        "first_seen": None,
        "last_seen": None,
    }


def _new_cell() -> dict[str, Any]:
    return {
        "n": 0,
        "durable": 0,
        "bad": 0,
        "pending": 0,
        "hours_to_merge": [],
        "cost_usd": [],
        "commits": [],
    }


def aggregate(
    prs: list[dict], facts: dict[str, dict[str, Any]], *, now: int, window_days: int
) -> dict[str, Any]:
    shapes: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    with_facts = 0
    for pr in prs:
        fact = facts.get(pr["ref"])
        if not fact or fact.get("unavailable"):
            missing.append(pr["ref"])
            continue
        with_facts += 1
        sig = shape_signature(fact.get("title"), fact.get("labels"), fact.get("paths"))
        shape = shapes.setdefault(sig["key"], _new_shape(sig))
        shape["prs"] += 1
        shape["repos"].add(pr["repo"])
        if len(shape["examples"]) < EXAMPLES_PER_SHAPE:
            shape["examples"].append(pr["ref"])
        merged_ts = fact.get("merged_ts") or pr["ts"]
        shape["first_seen"] = min(filter(None, [shape["first_seen"], merged_ts]))
        shape["last_seen"] = max(filter(None, [shape["last_seen"], merged_ts]))
        cell = shape["agents"].setdefault(pr["agent"], _new_cell())
        cell["n"] += 1
        if pr["durability"] == "durable":
            cell["durable"] += 1
        elif pr["durability"] in BAD_DURABILITY:
            cell["bad"] += 1
        else:
            cell["pending"] += 1
        created, merged = fact.get("created_ts"), fact.get("merged_ts")
        if created and merged and merged >= created:
            cell["hours_to_merge"].append((merged - created) / 3600)
        if pr.get("cost_usd") is not None:
            cell["cost_usd"].append(float(pr["cost_usd"]))
        if fact.get("commits") is not None:
            cell["commits"].append(float(fact["commits"]))
    rows: list[dict[str, Any]] = []
    for shape in shapes.values():
        repos = sorted(shape["repos"])
        agents: dict[str, dict[str, Any]] = {}
        for agent, cell in sorted(shape["agents"].items()):
            resolved = cell["durable"] + cell["bad"]
            agents[agent] = {
                "n": cell["n"],
                "durable": cell["durable"],
                "bad": cell["bad"],
                "pending": cell["pending"],
                "broke_later_rate": round(cell["bad"] / resolved, 3) if resolved else None,
                "hours_to_merge_median": _median(cell["hours_to_merge"]),
                "cost_usd_median": _median(cell["cost_usd"]),
                "commits_median": _median(cell["commits"]),
            }
        cross_repo = shape["prs"] >= RECURRING_MIN_PRS and len(repos) >= RECURRING_MIN_REPOS
        single_repo = shape["prs"] >= SINGLE_REPO_MIN_PRS
        because = ""
        if cross_repo:
            because = f"{shape['prs']} PRs across {len(repos)} repos"
        elif single_repo:
            because = f"{shape['prs']} PRs in {repos[0]}"
        rows.append(
            {
                **{k: v for k, v in shape.items() if k not in ("repos", "agents")},
                "repos": repos,
                "agents": agents,
                "recurring": bool(because),
                "recurring_because": because,
            }
        )
    rows.sort(key=lambda r: (-r["prs"], r["key"]))
    return {
        "schema": SCHEMA,
        "version": VERSION,
        "generated_at": now,
        "window_days": window_days,
        "counts": {
            "prs": len(prs),
            "with_facts": with_facts,
            "missing_facts": len(missing),
            "shapes": len(rows),
            "recurring": sum(1 for r in rows if r["recurring"]),
        },
        "missing_facts": missing[:MISSING_REPORTED_MAX],
        "definitions": {
            "shape": "commit type | label family | path classes (most-touched, at most 3)",
            "recurring": (
                f">= {RECURRING_MIN_PRS} PRs across >= {RECURRING_MIN_REPOS} repos, "
                f"or >= {SINGLE_REPO_MIN_PRS} in one repo"
            ),
            "bad_durability": list(BAD_DURABILITY),
            "rounds_proxy": ROUNDS_PROXY,
            "excluded": "bot and owner rows (attribution_source bot:*/human) and agent='none'",
        },
        "shapes": rows,
    }


# ---------------------------------------------------------------- run / render -------------------


def run(
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    state_dir: Path | None = None,
    fetch_limit: int = FETCH_LIMIT_DEFAULT,
    now: int | None = None,
    fetch_fn: Callable[[str, list[int]], dict[int, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    now = int(now or time.time())
    state_dir = state_dir or default_state_dir()
    prs = merged_agent_prs(window_days=window_days, now=now)
    facts = load_facts(state_dir)
    missing_by_repo: dict[str, list[int]] = {}
    for pr in prs:
        if pr["ref"] not in facts:
            missing_by_repo.setdefault(pr["repo"], []).append(pr["number"])
    fetch = fetch_fn or fetch_facts
    budget = max(0, int(fetch_limit))
    fetched = unavailable = 0
    for repo, numbers in sorted(missing_by_repo.items()):
        if budget <= 0:
            break
        take = numbers[:budget]
        budget -= len(take)
        got = fetch(repo, take)
        for number in take:
            fact = got.get(number)
            if fact:
                facts[f"{repo}#{number}"] = fact
                fetched += 1
            else:
                # Recorded so the same deleted PR is not asked for on every run; never a shape.
                facts[f"{repo}#{number}"] = {"unavailable": True, "attempted_ts": now}
                unavailable += 1
    if fetched or unavailable:
        save_facts(state_dir, facts, now=now)
    payload = aggregate(prs, facts, now=now, window_days=window_days)
    payload["counts"]["fetched_this_run"] = fetched
    payload["counts"]["unavailable_this_run"] = unavailable
    write_json_atomic(state_dir / "fleet-shapes.json", payload)
    (state_dir / "fleet-shapes.md").write_text(render_md(payload), encoding="utf-8")
    _LOOKUP_CACHE.pop(str(state_dir / "fleet-shapes.json"), None)
    return payload


def _agent_phrase(agent: str, cell: dict[str, Any]) -> str:
    rate = cell.get("broke_later_rate")
    hours = cell.get("hours_to_merge_median")
    bits = [f"{agent} {cell.get('n')}"]
    bits.append(f"broke-later {rate:.0%}" if rate is not None else "broke-later unmeasured")
    if hours is not None:
        bits.append(f"{hours:.1f}h to merge")
    return " ".join(bits[:1]) + " (" + ", ".join(bits[1:]) + ")"


def render_md(payload: dict[str, Any]) -> str:
    counts = payload.get("counts") or {}
    lines = [
        f"# Fleet work shapes — {payload.get('window_days')}d window",
        "",
        f"Merged agent PRs {counts.get('prs')}, with facts {counts.get('with_facts')}, "
        f"missing facts {counts.get('missing_facts')}, shapes {counts.get('shapes')}, "
        f"recurring {counts.get('recurring')}. Shape = commit type | label family | path classes. "
        f"Rounds proxy: {ROUNDS_PROXY}.",
        "",
        "| shape | PRs | repos | recurring | per agent |",
        "|---|---|---|---|---|",
    ]
    for row in payload.get("shapes") or []:
        agents = "; ".join(_agent_phrase(a, c) for a, c in (row.get("agents") or {}).items())
        lines.append(
            f"| `{row['key']}` | {row['prs']} | {len(row.get('repos') or [])} | "
            f"{'yes' if row.get('recurring') else 'no'} | {agents} |"
        )
    return "\n".join(lines) + "\n"


def recurring_shape(key: str, *, state_dir: Path | None = None) -> dict[str, Any] | None:
    """The recurring shape a signature key belongs to, or None. Cheap: the artifact is parsed once
    per mtime, so a consult that asks for several PRs reads the file once."""
    path = (state_dir or default_state_dir()) / "fleet-shapes.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    cached = _LOOKUP_CACHE.get(str(path))
    if cached is None or cached[0] != mtime:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        index = {
            str(row.get("key")): row
            for row in (payload.get("shapes") or [])
            if isinstance(row, dict) and row.get("recurring")
        }
        cached = (mtime, {"index": index, "window_days": payload.get("window_days")})
        _LOOKUP_CACHE[str(path)] = cached
    row = cached[1]["index"].get(key)
    if not row:
        return None
    return {**row, "window_days": cached[1]["window_days"]}


def summary_for_report(state_dir: Path | None = None, *, top: int = 5) -> dict[str, Any]:
    """What the periodic report prints: the counts and the top recurring shapes, or the named
    absence when the cadence has not produced the artifact yet."""
    path = (state_dir or default_state_dir()) / "fleet-shapes.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "not yet mined", "artifact": str(path)}
    rows = [
        {
            "key": row.get("key"),
            "prs": row.get("prs"),
            "repos": len(row.get("repos") or []),
            "agents": {
                agent: {
                    "n": cell.get("n"),
                    "broke_later_rate": cell.get("broke_later_rate"),
                    "hours_to_merge_median": cell.get("hours_to_merge_median"),
                }
                for agent, cell in (row.get("agents") or {}).items()
            },
        }
        for row in payload.get("shapes") or []
        if isinstance(row, dict) and row.get("recurring")
    ][:top]
    return {
        "state": "mined",
        "generated_at": payload.get("generated_at"),
        "window_days": payload.get("window_days"),
        "counts": payload.get("counts") or {},
        "top_recurring": rows,
    }


def render_report_lines(summary: dict[str, Any]) -> list[str]:
    if summary.get("state") != "mined":
        return [f"FLEET-SHAPES: {summary.get('state', 'unknown')} ({summary.get('artifact', '')})"]
    counts = summary.get("counts") or {}
    lines = [
        f"FLEET-SHAPES ({summary.get('window_days')}d): {counts.get('prs')} merged agent PRs, "
        f"facts {counts.get('with_facts')}, missing {counts.get('missing_facts')}, "
        f"{counts.get('shapes')} shapes, {counts.get('recurring')} recurring"
    ]
    for row in summary.get("top_recurring") or []:
        agents = "; ".join(_agent_phrase(a, c) for a, c in (row.get("agents") or {}).items())
        lines.append(f"  {row['key']}: {row['prs']} PRs / {row['repos']} repos — {agents}")
    return lines


# ---------------------------------------------------------------- selftest / cli -----------------


def _selftest() -> int:
    failures: list[str] = []

    def check(cond: bool, what: str) -> None:
        if not cond:
            failures.append(what)

    check(path_class(".github/workflows/ci.yml") == "workflows", "workflows class")
    check(path_class(".github/scripts/x.js") == "github-meta", "github-meta class")
    check(path_class("tests/test_x.py") == "tests", "tests class by dir")
    check(path_class("pkg/foo_test.py") == "tests", "tests class by suffix")
    check(path_class("docs/guide.md") == "docs" and path_class("README.md") == "docs", "docs class")
    check(path_class("scripts/run.py") == "scripts", "scripts class")
    check(
        path_class("pyproject.toml") == "config" and path_class(".gitignore") == "config", "config"
    )
    check(path_class("src/app/main.py") == "code" and path_class("main.py") == "code", "code class")
    check(path_class(".agents/ledger.yml") == "bookkeeping", "bookkeeping class")
    sig = shape_signature(
        "fix: guard the null path",
        ["agent:codex", "bug", "status:ready", "Area:API"],
        ["src/a.py", "src/b.py", "tests/test_a.py", "README.md", "docs/x.md"],
    )
    check(sig["commit_type"] == "fix", f"commit type {sig['commit_type']!r}")
    check(sig["labels"] == ["area:api", "bug"], f"label family {sig['labels']!r}")
    check(sig["path_classes"] == ["code", "docs", "tests"], f"path classes {sig['path_classes']!r}")
    check(sig["key"] == "fix|area:api,bug|code+docs+tests", f"key {sig['key']!r}")
    check(commit_type("Docs: tidy") == "docs" and commit_type("Random title") == "other", "aliases")
    now = 1_800_000_000
    prs: list[dict[str, Any]] = [
        {
            "ref": f"o/r{i % 2}#{i}",
            "repo": f"o/r{i % 2}",
            "number": i,
            "agent": "codex" if i < 3 else "claude",
            "ts": now - 86400 * i,
            "durability": "broke_later" if i == 1 else "durable",
            "verifier": "PASS",
            "cost_usd": 0.5,
        }
        for i in range(4)
    ]
    prs.append(
        {
            "ref": "o/r9#99",
            "repo": "o/r9",
            "number": 99,
            "agent": "codex",
            "ts": now,
            "durability": "pending",
            "verifier": None,
            "cost_usd": None,
        }
    )
    facts: dict[str, dict[str, Any]] = {
        str(pr["ref"]): {
            "title": "chore: bump deps",
            "labels": ["dependencies"],
            "paths": ["pyproject.toml"],
            "created_ts": int(pr["ts"]) - 7200,
            "merged_ts": int(pr["ts"]),
            "commits": 2,
        }
        for pr in prs[:4]
    }
    payload = aggregate(prs, facts, now=now, window_days=60)
    counts = payload["counts"]
    check(
        counts == {"prs": 5, "with_facts": 4, "missing_facts": 1, "shapes": 1, "recurring": 1},
        f"counts {counts}",
    )
    shape = payload["shapes"][0]
    check(shape["recurring"] and shape["repos"] == ["o/r0", "o/r1"], "cross-repo recurrence")
    codex = shape["agents"]["codex"]
    check(
        codex
        == {
            "n": 3,
            "durable": 2,
            "bad": 1,
            "pending": 0,
            "broke_later_rate": 0.333,
            "hours_to_merge_median": 2.0,
            "cost_usd_median": 0.5,
            "commits_median": 2.0,
        },
        f"codex cell {codex}",
    )
    check(payload["missing_facts"] == ["o/r9#99"], "missing facts named")
    check("chore|dependencies|config" in render_md(payload), "markdown names the shape")
    check(
        render_report_lines({"state": "not yet mined", "artifact": "x"})[0].startswith(
            "FLEET-SHAPES: not yet mined"
        ),
        "unmined line",
    )
    if failures:
        print("fleet_shapes.py selftest: FAIL — " + "; ".join(failures))
        return 1
    print(
        "fleet_shapes.py selftest: OK (path classes, signature with dropped queue labels, cross-repo "
        "recurrence, per-agent broke-later/hours/cost/commits, missing facts named, render lines)"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return _selftest()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="mine merged fleet PRs into shapes and write the artifacts")
    run_p.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    run_p.add_argument("--state-dir", type=Path, default=None)
    run_p.add_argument("--fetch-limit", type=int, default=FETCH_LIMIT_DEFAULT)
    run_p.add_argument("--json", action="store_true")
    show_p = sub.add_parser("show", help="print the current shapes")
    show_p.add_argument("--top", type=int, default=10)
    show_p.add_argument("--state-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.command == "run":
        payload = run(
            window_days=args.window_days, state_dir=args.state_dir, fetch_limit=args.fetch_limit
        )
        if args.json:
            print(json.dumps({k: v for k, v in payload.items() if k != "shapes"}, indent=1))
        else:
            print("\n".join(render_report_lines(summary_for_report(args.state_dir, top=10))))
        return 0
    summary = summary_for_report(args.state_dir, top=args.top)
    print("\n".join(render_report_lines(summary)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
