#!/usr/bin/env python3
"""Paired agent-switch observations from the fleet's own label history, plus the sampling switch.

WHY THIS EXISTS (the 2026-09-15 evaluation, "beyond what has been tried"). The only natural experiment
for comparative advantage is an `agent:auto` switch: two LLMs touch the same task, and the second one's
result is measured against the first one's stall. The keepalive delegation policy already performs the
switch (agent_delegation_policy.js: `agent:auto` present, two consecutive rounds without progress,
five-round cooldown), but nothing recorded the pair, and the base rate measured on 2026-09-17 was
7 switches among 986 merged agent PRs in 60 days, with `agent:auto` on 14 of them and none of those
14 switching. So there are two halves here:

1. RECORD every switch as a paired observation. For each keepalive PR in the window the module reads
   the `agent:*` label timeline and the commit dates (one GraphQL query per 25 PRs, cached per PR in
   `<state>/agent-switches-facts.json`), derives the consecutive distinct agents that were applied, and
   writes each from→to pair to the Brain table `agent_switches` with commits before and after the
   switch and the PR's terminal outcome. Bot and owner rows are excluded by attribution_source.
2. SAMPLE, default OFF. `sample --rate R --apply` assigns eligible open fleet PRs (exactly one real
   agent label, no `agent:auto`, no `agent:rate-limited`) to the `auto` arm by a stable hash of the PR
   reference and adds `agent:auto` to that arm; the rest are the control arm. Assignments are recorded
   only when the label is actually applied, so the arms in the record are the arms that ran. The tick
   runs this only when `ORCH_AUTO_SWITCH_SAMPLE_RATE` is set above zero.

DEDUP (2026-09-17). Searched the tree for agent:auto (the ingest excludes it from attribution and the
closer adds it to capacity-stuck PRs), agent_switch / paired (absent), the Brain (no table holds label
history or switches) and the improvement log (no item). Not present; built.

This module never removes a label, never picks the replacement agent (the delegation policy does, from
the exported route weights), never writes a review queue, and applies nothing unless `--apply` is
given with a positive rate. Kill switch for the record half: `ORCH_DISABLE_STEPS=agent-switches`.

    python3 agent_switches.py run [--window-days 60] [--state-dir DIR] [--fetch-limit 300] [--json]
    python3 agent_switches.py sample --rate 0.25 [--apply] [--state-dir DIR] [--json]
    python3 agent_switches.py show [--state-dir DIR]
    python3 agent_switches.py --selftest
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import feedback

SCHEMA = "orchestrator.agent-switches"
VERSION = 1
FACTS_SCHEMA = "orchestrator.agent-switches-facts"
DEFAULT_WINDOW_DAYS = 60
FETCH_LIMIT_DEFAULT = 300
GRAPHQL_CHUNK = 25
# Eligibility reaches back a month: a PR that has sat open for weeks is exactly where a stall lives.
SAMPLE_MAX_AGE_DAYS = 30
SAMPLE_RATE_ENV = "ORCH_AUTO_SWITCH_SAMPLE_RATE"
AGENT_LABELS = (
    "agent:codex",
    "agent:claude",
    "agent:cursor",
    "agent:gemini",
    "agent:vibe",
    "agent:aider",
    "agent:copilot",
)
AUTO_LABEL = "agent:auto"
RATE_LIMITED_LABEL = "agent:rate-limited"
BAD_DURABILITY = ("broke_later", "reverted", "reopened", "abandoned", "reworked")
MISSING_REPORTED_MAX = 50


def default_state_dir() -> Path:
    return Path(os.environ.get("ORCH_STATE_DIR", Path.home() / ".codex" / "orchestrator"))


# ---------------------------------------------------------------- brain --------------------------


def keepalive_prs(*, window_days: int = DEFAULT_WINDOW_DAYS, now: int | None = None) -> list[dict]:
    """Every keepalive PR row in the window — merged or not, attributed or not — because a switch is
    visible in the labels whatever the ingest concluded. Bot and owner rows are excluded."""
    now = int(now or time.time())
    since = now - window_days * 86400
    out: list[dict] = []
    with feedback._conn() as c:
        rows = c.execute(
            "SELECT r.target, r.agent, r.ts, r.routing_metadata, o.merged, o.durability "
            "FROM runs r LEFT JOIN outcomes o ON o.run_id=r.run_id "
            "WHERE r.source='keepalive' AND r.ts>=? ORDER BY r.ts",
            (since,),
        ).fetchall()
    seen: set[str] = set()
    for target, agent, ts, metadata_raw, merged, durability in rows:
        source = str(feedback._routing_metadata_dict(metadata_raw).get("attribution_source") or "")
        if source.startswith("bot:") or source == "human":
            continue
        repo, _, number = str(target or "").partition("#")
        if not repo or not number.isdigit():
            continue
        ref = f"{repo}#{number}"
        if ref in seen:
            continue
        seen.add(ref)
        out.append(
            {
                "ref": ref,
                "repo": repo,
                "number": int(number),
                "agent": str(agent or "none"),
                "ts": int(ts),
                "merged": bool(merged),
                "durability": str(durability or "pending"),
            }
        )
    return out


def fleet_repos(prs: list[dict]) -> list[str]:
    return sorted({pr["repo"] for pr in prs})


# ---------------------------------------------------------------- github facts -------------------


def _gh_json(args: list[str], *, timeout: int = 180) -> object | None:
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
    events: list[dict[str, Any]] = []
    timeline = pr.get("timelineItems") if isinstance(pr.get("timelineItems"), dict) else {}
    for node in (timeline or {}).get("nodes") or []:
        if not isinstance(node, dict):
            continue
        label = node.get("label") if isinstance(node.get("label"), dict) else {}
        name = str((label or {}).get("name") or "")
        if not name.startswith("agent:"):
            continue
        events.append(
            {
                "ts": _epoch(node.get("createdAt")),
                "kind": "added" if node.get("__typename") == "LabeledEvent" else "removed",
                "label": name,
            }
        )
    commits: list[int] = []
    commits_node = pr.get("commits") if isinstance(pr.get("commits"), dict) else {}
    for node in (commits_node or {}).get("nodes") or []:
        commit = node.get("commit") if isinstance(node, dict) else None
        ts = _epoch((commit or {}).get("committedDate")) if isinstance(commit, dict) else None
        if ts:
            commits.append(ts)
    return {
        "label_events": events,
        "commit_ts": sorted(commits),
        "state": str(pr.get("state") or ""),
        "merged_ts": _epoch(pr.get("mergedAt")),
        "closed_ts": _epoch(pr.get("closedAt")),
    }


def fetch_facts(
    repo: str,
    numbers: list[int],
    *,
    gh_json: Callable[[list[str]], object | None] = _gh_json,
) -> dict[int, dict[str, Any]]:
    """Label timeline and commit dates, one GraphQL query per 25 PRs; a failed chunk is retried one PR
    at a time so a deleted PR costs only itself."""
    owner, _, name = repo.partition("/")
    out: dict[int, dict[str, Any]] = {}
    for i in range(0, len(numbers), GRAPHQL_CHUNK):
        chunk = numbers[i : i + GRAPHQL_CHUNK]
        fields = " ".join(
            f"p{n}: pullRequest(number:{n}){{ number state mergedAt closedAt "
            f"timelineItems(itemTypes:[LABELED_EVENT,UNLABELED_EVENT], first:80){{ nodes {{ "
            f"__typename ... on LabeledEvent {{ createdAt label{{name}} }} "
            f"... on UnlabeledEvent {{ createdAt label{{name}} }} }} }} "
            f"commits(first:100){{ nodes {{ commit {{ committedDate }} }} }} }}"
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
    try:
        payload = json.loads((state_dir / "agent-switches-facts.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    facts = payload.get("facts") if isinstance(payload, dict) else None
    return {str(k): v for k, v in (facts or {}).items() if isinstance(v, dict)}


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
        state_dir / "agent-switches-facts.json",
        {"schema": FACTS_SCHEMA, "version": VERSION, "updated_at": now, "facts": facts},
    )


# ---------------------------------------------------------------- derive -------------------------


def derive_switches(fact: dict[str, Any]) -> dict[str, Any]:
    """From one PR's label events: the ordered distinct real agents applied, each switch between
    consecutive ones (timestamped by the arriving label), commits before/after each switch, and
    whether `agent:auto` was ever applied. Removals do not end an agent's turn — the arriving label
    does — because the policy removes the old label and adds the new one in the same round."""
    added = sorted(
        (int(e["ts"]), str(e["label"]))
        for e in fact.get("label_events") or []
        if e.get("kind") == "added" and e.get("ts") and str(e.get("label")) in AGENT_LABELS
    )
    auto = any(
        e.get("kind") == "added" and str(e.get("label")) == AUTO_LABEL
        for e in fact.get("label_events") or []
    )
    sequence: list[tuple[int, str]] = []
    for ts, label in added:
        agent = label.split(":", 1)[1]
        if not sequence or sequence[-1][1] != agent:
            sequence.append((ts, agent))
    commits = [int(t) for t in fact.get("commit_ts") or []]
    switches: list[dict[str, Any]] = []
    for (_, from_agent), (ts, to_agent) in zip(sequence, sequence[1:]):
        switches.append(
            {
                "from_agent": from_agent,
                "to_agent": to_agent,
                "switched_ts": ts,
                "commits_before": sum(1 for c in commits if c < ts),
                "commits_after": sum(1 for c in commits if c >= ts),
            }
        )
    return {
        "agents": [agent for _, agent in sequence],
        "auto_label": auto,
        "switches": switches,
    }


def _record(pr: dict, derived: dict[str, Any], *, now: int) -> None:
    with feedback._conn() as c:
        for sw in derived["switches"]:
            c.execute(
                "INSERT OR REPLACE INTO agent_switches (pr_ref, from_agent, to_agent, switched_ts, "
                "auto_label, commits_before, commits_after, merged, durability, recorded_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    pr["ref"],
                    sw["from_agent"],
                    sw["to_agent"],
                    int(sw["switched_ts"]),
                    1 if derived["auto_label"] else 0,
                    int(sw["commits_before"]),
                    int(sw["commits_after"]),
                    1 if pr.get("merged") else 0,
                    pr.get("durability"),
                    now,
                ),
            )


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def aggregate(
    prs: list[dict], facts: dict[str, dict[str, Any]], *, now: int, window_days: int
) -> tuple[dict[str, Any], list[tuple[dict, dict[str, Any]]]]:
    missing: list[str] = []
    with_facts = switched_prs = auto_labeled = auto_and_switched = 0
    transitions: dict[str, dict[str, Any]] = {}
    recordable: list[tuple[dict, dict[str, Any]]] = []
    for pr in prs:
        fact = facts.get(pr["ref"])
        if not fact or fact.get("unavailable"):
            missing.append(pr["ref"])
            continue
        with_facts += 1
        derived = derive_switches(fact)
        auto_labeled += 1 if derived["auto_label"] else 0
        if not derived["switches"]:
            continue
        switched_prs += 1
        auto_and_switched += 1 if derived["auto_label"] else 0
        recordable.append((pr, derived))
        for sw in derived["switches"]:
            key = f"{sw['from_agent']}->{sw['to_agent']}"
            cell = transitions.setdefault(
                key,
                {
                    "n": 0,
                    "merged": 0,
                    "durable": 0,
                    "bad": 0,
                    "auto_label": 0,
                    "commits_before": [],
                    "commits_after": [],
                    "examples": [],
                },
            )
            cell["n"] += 1
            cell["merged"] += 1 if pr.get("merged") else 0
            cell["durable"] += 1 if pr.get("durability") == "durable" else 0
            cell["bad"] += 1 if pr.get("durability") in BAD_DURABILITY else 0
            cell["auto_label"] += 1 if derived["auto_label"] else 0
            cell["commits_before"].append(float(sw["commits_before"]))
            cell["commits_after"].append(float(sw["commits_after"]))
            if len(cell["examples"]) < 5:
                cell["examples"].append(pr["ref"])
    rows = {
        key: {
            **{k: v for k, v in cell.items() if k not in ("commits_before", "commits_after")},
            "commits_before_median": _median(cell["commits_before"]),
            "commits_after_median": _median(cell["commits_after"]),
        }
        for key, cell in sorted(transitions.items(), key=lambda kv: (-kv[1]["n"], kv[0]))
    }
    payload = {
        "schema": SCHEMA,
        "version": VERSION,
        "generated_at": now,
        "window_days": window_days,
        "counts": {
            "prs": len(prs),
            "with_facts": with_facts,
            "missing_facts": len(missing),
            "switched_prs": switched_prs,
            "switches": sum(cell["n"] for cell in transitions.values()),
            "auto_labeled": auto_labeled,
            "auto_and_switched": auto_and_switched,
        },
        "missing_facts": missing[:MISSING_REPORTED_MAX],
        "transitions": rows,
        "definitions": {
            "switch": "consecutive distinct real agent labels applied to one PR; agent:auto and "
            "agent:rate-limited are not agents",
            "commits_before_after": "commit dates against the arriving label's timestamp",
            "excluded": "bot and owner rows (attribution_source bot:*/human)",
            "policy": "agent_delegation_policy.js switches on agent:auto after two rounds without "
            "progress, five-round cooldown",
        },
    }
    return payload, recordable


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
    prs = keepalive_prs(window_days=window_days, now=now)
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
                facts[f"{repo}#{number}"] = {"unavailable": True, "attempted_ts": now}
                unavailable += 1
    if fetched or unavailable:
        save_facts(state_dir, facts, now=now)
    payload, recordable = aggregate(prs, facts, now=now, window_days=window_days)
    for pr, derived in recordable:
        _record(pr, derived, now=now)
    payload["counts"]["fetched_this_run"] = fetched
    payload["counts"]["unavailable_this_run"] = unavailable
    payload["counts"]["recorded_in_brain"] = sum(len(d["switches"]) for _, d in recordable)
    existing = load_state(state_dir)
    payload["sample"] = existing.get("sample") or {"rate": 0, "assignments": {}}
    write_json_atomic(state_dir / "agent-switches.json", payload)
    return payload


def load_state(state_dir: Path) -> dict[str, Any]:
    try:
        payload = json.loads((state_dir / "agent-switches.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


# ---------------------------------------------------------------- sample -------------------------


def arm_for(ref: str, rate: float) -> str:
    """Stable assignment: the same PR lands in the same arm on every run, at the given rate."""
    if rate <= 0:
        return "control"
    bucket = int(hashlib.sha1(ref.encode("utf-8")).hexdigest(), 16) % 10_000
    return "auto" if bucket < int(round(rate * 10_000)) else "control"


def eligible_open_prs(
    repos: list[str],
    *,
    now: int,
    max_age_days: int = SAMPLE_MAX_AGE_DAYS,
    gh_json: Callable[[list[str]], object | None] = _gh_json,
) -> list[dict[str, Any]]:
    """Open fleet PRs carrying exactly one real agent label and neither agent:auto nor
    agent:rate-limited, opened within max_age_days. One search per agent label."""
    out: dict[str, dict[str, Any]] = {}
    wanted = set(repos)
    for label in AGENT_LABELS:
        query = (
            f'search(query:"org:stranske is:pr is:open archived:false label:\\"{label}\\"", '
            "type:ISSUE, first:100){ nodes { ... on PullRequest { number createdAt "
            "repository{nameWithOwner} labels(first:30){nodes{name}} } } }"
        )
        data = gh_json(["gh", "api", "graphql", "-f", f"query=query {{ {query} }}"])
        payload = data.get("data") if isinstance(data, dict) else None
        search = payload.get("search") if isinstance(payload, dict) else None
        for node in (search or {}).get("nodes") or []:
            if not isinstance(node, dict) or node.get("number") is None:
                continue
            repo = str((node.get("repository") or {}).get("nameWithOwner") or "")
            if repo not in wanted:
                continue
            labels = [
                str(n.get("name"))
                for n in ((node.get("labels") or {}).get("nodes") or [])
                if isinstance(n, dict) and n.get("name")
            ]
            real = [lab for lab in labels if lab in AGENT_LABELS]
            created = _epoch(node.get("createdAt")) or 0
            if len(real) != 1 or AUTO_LABEL in labels or RATE_LIMITED_LABEL in labels:
                continue
            if now - created > max_age_days * 86400:
                continue
            ref = f"{repo}#{int(node['number'])}"
            out[ref] = {"ref": ref, "repo": repo, "number": int(node["number"]), "agent": real[0]}
    return [out[k] for k in sorted(out)]


def _apply_auto_label(ref: str) -> bool:
    repo, _, number = ref.partition("#")
    try:
        proc = subprocess.run(
            ["gh", "pr", "edit", number, "-R", repo, "--add-label", AUTO_LABEL],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


def sample(
    *,
    rate: float,
    apply: bool = False,
    state_dir: Path | None = None,
    now: int | None = None,
    candidates: list[dict[str, Any]] | None = None,
    apply_fn: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Assign eligible open PRs to arms and, with apply=True and a positive rate, add agent:auto to
    the auto arm. Dry runs report what would happen and persist nothing: an arm is recorded only when
    its label was actually applied, so the recorded arms are the arms that ran."""
    now = int(now or time.time())
    state_dir = state_dir or default_state_dir()
    state = load_state(state_dir)
    sample_state = state.get("sample") or {}
    assignments: dict[str, dict[str, Any]] = dict(sample_state.get("assignments") or {})
    if candidates is None:
        repos = fleet_repos(keepalive_prs(window_days=DEFAULT_WINDOW_DAYS, now=now))
        candidates = eligible_open_prs(repos, now=now)
    applier = apply_fn or _apply_auto_label
    would_auto = would_control = applied = failed = already = 0
    for pr in candidates:
        ref = pr["ref"]
        if ref in assignments:
            already += 1
            continue
        arm = arm_for(ref, rate)
        if arm == "auto":
            would_auto += 1
        else:
            would_control += 1
        if not apply or rate <= 0:
            continue
        if arm == "auto":
            if not applier(ref):
                failed += 1
                continue
            applied += 1
        assignments[ref] = {
            "arm": arm,
            "agent": pr.get("agent"),
            "assigned_ts": now,
            "applied": arm == "auto",
        }
    summary = {
        "rate": rate,
        "apply": bool(apply),
        "candidates": len(candidates),
        "already_assigned": already,
        "would_auto": would_auto,
        "would_control": would_control,
        "applied_now": applied,
        "apply_failed": failed,
        "assigned_total": len(assignments),
        "auto_total": sum(1 for a in assignments.values() if a.get("arm") == "auto"),
        "control_total": sum(1 for a in assignments.values() if a.get("arm") == "control"),
    }
    if apply and rate > 0:
        state["sample"] = {"rate": rate, "updated_at": now, "assignments": assignments}
        state.setdefault("schema", SCHEMA)
        state.setdefault("version", VERSION)
        write_json_atomic(state_dir / "agent-switches.json", state)
    return summary


# ---------------------------------------------------------------- report -------------------------


def summary_for_report(state_dir: Path | None = None) -> dict[str, Any]:
    path = (state_dir or default_state_dir()) / "agent-switches.json"
    payload = load_state(state_dir or default_state_dir())
    if not payload or "counts" not in payload:
        return {"state": "not yet recorded", "artifact": str(path)}
    sample_state = payload.get("sample") or {}
    assignments = sample_state.get("assignments") or {}
    return {
        "state": "recorded",
        "generated_at": payload.get("generated_at"),
        "window_days": payload.get("window_days"),
        "counts": payload.get("counts") or {},
        "transitions": {
            k: {"n": v.get("n"), "merged": v.get("merged"), "bad": v.get("bad")}
            for k, v in (payload.get("transitions") or {}).items()
        },
        "sample": {
            "rate": sample_state.get("rate", 0),
            "auto": sum(1 for a in assignments.values() if a.get("arm") == "auto"),
            "control": sum(1 for a in assignments.values() if a.get("arm") == "control"),
        },
    }


def render_report_lines(summary: dict[str, Any]) -> list[str]:
    if summary.get("state") != "recorded":
        return [
            f"AGENT-SWITCHES: {summary.get('state', 'unknown')} ({summary.get('artifact', '')})"
        ]
    c = summary.get("counts") or {}
    s = summary.get("sample") or {}
    lines = [
        f"AGENT-SWITCHES ({summary.get('window_days')}d): {c.get('switched_prs')} of "
        f"{c.get('with_facts')} keepalive PRs with facts switched agents ({c.get('switches')} "
        f"switches; {c.get('missing_facts')} facts missing); agent:auto on {c.get('auto_labeled')}, "
        f"of which {c.get('auto_and_switched')} switched; sampling rate {s.get('rate', 0)} "
        f"(auto arm {s.get('auto', 0)}, control {s.get('control', 0)})"
    ]
    for key, cell in (summary.get("transitions") or {}).items():
        lines.append(
            f"  {key}: {cell.get('n')} (merged {cell.get('merged')}, bad {cell.get('bad')})"
        )
    return lines


# ---------------------------------------------------------------- selftest / cli -----------------


def _selftest() -> int:
    failures: list[str] = []

    def check(cond: bool, what: str) -> None:
        if not cond:
            failures.append(what)

    fact = {
        "label_events": [
            {"ts": 100, "kind": "added", "label": "agent:cursor"},
            {"ts": 150, "kind": "added", "label": "agent:auto"},
            {"ts": 200, "kind": "removed", "label": "agent:cursor"},
            {"ts": 200, "kind": "added", "label": "agent:codex"},
            {"ts": 260, "kind": "added", "label": "agent:codex"},
            {"ts": 300, "kind": "added", "label": "agent:rate-limited"},
            {"ts": 400, "kind": "added", "label": "agent:claude"},
        ],
        "commit_ts": [120, 180, 250, 450],
    }
    d = derive_switches(fact)
    check(d["agents"] == ["cursor", "codex", "claude"], f"sequence {d['agents']}")
    check(d["auto_label"] is True, "auto label seen")
    check(len(d["switches"]) == 2, f"switch count {len(d['switches'])}")
    first = d["switches"][0]
    check(
        (first["from_agent"], first["to_agent"], first["switched_ts"]) == ("cursor", "codex", 200),
        f"first switch {first}",
    )
    check((first["commits_before"], first["commits_after"]) == (2, 2), f"commit split {first}")
    check(derive_switches({"label_events": [], "commit_ts": []})["switches"] == [], "no events")
    arms = {arm_for(f"o/r#{i}", 0.25) for i in range(200)}
    check(arms == {"auto", "control"}, "both arms occur at 25%")
    share = sum(1 for i in range(2000) if arm_for(f"o/r#{i}", 0.25) == "auto") / 2000
    check(0.20 < share < 0.30, f"auto share {share}")
    check(arm_for("o/r#1", 0.25) == arm_for("o/r#1", 0.25), "assignment is stable")
    check(all(arm_for(f"o/r#{i}", 0) == "control" for i in range(50)), "rate 0 is all control")
    line = render_report_lines({"state": "not yet recorded", "artifact": "x"})[0]
    check(line.startswith("AGENT-SWITCHES: not yet recorded"), "unrecorded line")
    if failures:
        print("agent_switches.py selftest: FAIL — " + "; ".join(failures))
        return 1
    print(
        "agent_switches.py selftest: OK (switch derivation ignores auto/rate-limited and collapses "
        "repeats, commits split at the arriving label, stable hashed arms at the given rate, "
        "unrecorded line named)"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return _selftest()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="record every agent switch in the window")
    run_p.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    run_p.add_argument("--state-dir", type=Path, default=None)
    run_p.add_argument("--fetch-limit", type=int, default=FETCH_LIMIT_DEFAULT)
    run_p.add_argument("--json", action="store_true")
    sample_p = sub.add_parser(
        "sample", help="assign eligible open PRs to arms (dry run by default)"
    )
    sample_p.add_argument("--rate", type=float, required=True)
    sample_p.add_argument("--apply", action="store_true")
    sample_p.add_argument("--state-dir", type=Path, default=None)
    sample_p.add_argument("--json", action="store_true")
    show_p = sub.add_parser("show", help="print the current summary")
    show_p.add_argument("--state-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.command == "run":
        payload = run(
            window_days=args.window_days, state_dir=args.state_dir, fetch_limit=args.fetch_limit
        )
        if args.json:
            print(json.dumps({k: v for k, v in payload.items() if k != "sample"}, indent=1))
        else:
            print("\n".join(render_report_lines(summary_for_report(args.state_dir))))
        return 0
    if args.command == "sample":
        summary = sample(rate=args.rate, apply=args.apply, state_dir=args.state_dir)
        print(json.dumps(summary, indent=1) if args.json else json.dumps(summary))
        return 0
    print("\n".join(render_report_lines(summary_for_report(args.state_dir))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
