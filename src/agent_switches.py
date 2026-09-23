#!/usr/bin/env python3
"""Paired agent-switch observations from the fleet's label history and keepalive's delegation log.

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
   POLICY SWITCHES (2026-09-23). The delegation policy never relabels: keepalive_loop.js persists
   each switch it makes only in its state marker (`<!-- keepalive-state:v1 {json} -->` in the
   bot-authored summary comment), as a `delegation_log` entry {iteration, previous_agent,
   chosen_agent, reason, timestamp}. A label timeline therefore cannot show one, and every row the
   table held on 2026-09-23 was a label swap by some other actor (`auto_label = 0` on all ten). The
   same GraphQL read that fetches the label timeline now also reads the PR's last 100 comments and
   keeps the latest marker from a TRUSTED writer, by the rule keepalive itself reads by
   (keepalive_state.js `isTrustedKeepaliveStateComment`; GoalsAndPlumbing.md §4). Each log entry
   becomes a `source='policy'` row carrying that entry's `delegation_source` — `route_weights`,
   `static` or `unknown` — parsed from the suffix the policy appends to the entry's reason, because
   the state's top-level `delegation_source` is overwritten every ROUND and says nothing about any
   one switch. Label rows are written exactly as before, with `source='label'`. A fact cached before
   this read gets the state alone, from the same fetch budget, and its label timeline is never
   re-read. Until a PR's state has been read it counts as UNREAD, never as zero switches.
2. SAMPLE — RETIRED 2026-09-22. A `sample` step used to add `agent:auto` to a hashed share of open
   fleet PRs that carried exactly one real agent label. The owner then had the opener label EVERY PR it
   creates `agent:auto` at creation, so that the route weights are consumed on auto stalls. That left
   the sample with no eligible population (it reported `candidates: 0` on every tick) and no untreated
   arm: the only unlabelled agent PRs left come from other origins, so comparing them with opener PRs
   measures origin, not the label. `derive_switches` still records whether `agent:auto` was ever
   applied (`auto_label`), which is the adoption signal the sample was meant to create. The two arms
   assigned before retirement stay in `agent-switches.json` as history; nothing reads the old
   `ORCH_AUTO_SWITCH_SAMPLE_RATE` any more.

DEDUP (2026-09-17). Searched the tree for agent:auto (the ingest excludes it from attribution and the
closer adds it to capacity-stuck PRs), agent_switch / paired (absent), the Brain (no table holds label
history or switches) and the improvement log (no item). Not present; built.
DEDUP (2026-09-23, the policy half). Searched the tree for keepalive-state, delegation_log,
delegation_source and switch_count, the improvement log for the same, and the open PRs.
keepalive_shadow.py already parses the marker, and its STATE_REGEX is reused here. It reads every
author's comments, though, and takes the last payload in the concatenated text, so the trusted-writer
selection is new. Nothing read delegation_log or delegation_source; the log's only mention is the
2026-09-23 caveat that asked for this; no open PR touched it. Extended this rail and its table (a
feedback.py migration) rather than adding a store.

This module never adds or removes a label, never picks the replacement agent (the delegation policy
does, from the exported route weights), and never writes a review queue. Kill switch:
`ORCH_DISABLE_STEPS=agent-switches`.

    python3 agent_switches.py run [--window-days 60] [--state-dir DIR] [--fetch-limit 300] [--json]
    python3 agent_switches.py show [--state-dir DIR]
    python3 agent_switches.py tick-line [--state-dir DIR]
    python3 agent_switches.py --selftest
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
import keepalive_shadow

SCHEMA = "orchestrator.agent-switches"
VERSION = 1
FACTS_SCHEMA = "orchestrator.agent-switches-facts"
DEFAULT_WINDOW_DAYS = 60
FETCH_LIMIT_DEFAULT = 300
GRAPHQL_CHUNK = 25
# The hashed `sample` step was retired on 2026-09-22 (see the module docstring); this is the date the
# report line names.
SAMPLE_RETIRED = "2026-09-22"
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
SOURCE_LABEL = "label"
SOURCE_POLICY = "policy"
# Who may write the keepalive state marker: Workflows .github/scripts/keepalive_state.js
# `isTrustedKeepaliveStateComment` and docs/keepalive/GoalsAndPlumbing.md §4 — the two dedicated Apps,
# the migration-only legacy Actions bot, and the two identity-checked PAT fallbacks. The check is
# TYPE-AWARE, as keepalive's is: a Bot login never stands in for a User login or the reverse.
TRUSTED_STATE_BOTS = frozenset(
    {"stranske-keepalive[bot]", "agents-workflows-bot[bot]", "github-actions[bot]"}
)
TRUSTED_STATE_USERS = frozenset({"stranske", "stranske-automation-bot"})
# The newest 100 comments. Keepalive reads the LAST trusted marker, so the newest page is the one that
# holds it; when it does not and older comments exist, the read is `truncated`, never `no_marker`.
STATE_COMMENTS_LAST = 100
# ONE selection, spliced into the facts query and the state-only backfill query, so they cannot drift.
STATE_SELECTION = (
    f" comments(last:{STATE_COMMENTS_LAST}){{ pageInfo {{ hasPreviousPage }} "
    "nodes { author { __typename login } body } }"
)
# A PR's comments that could not be read are retried by the backfill, at most this many reads in all.
STATE_READ_ATTEMPTS = 3
# `read` values that are a MEASUREMENT: a trusted marker was read, or every comment was read and none
# carried one. Every other value (and a fact with no state at all) is unread — never zero switches.
STATE_READ_MEASURED = ("marker", "no_marker")
# agent_delegation_policy.js appends `delegation_source: route_weights` or `delegation_source: static
# (<why>)` to the reason of every stall switch. Its only other switch, `<agent>-unavailable`, never
# consults the route weights and returns delegationSource 'static'. Anything else — a reason from a
# policy version older than the suffix — is `unknown`, because guessing would inflate either count.
DELEGATION_SOURCE_RE = re.compile(r"delegation_source:\s*(route_weights|static)\b")
UNAVAILABLE_REASON_RE = re.compile(r"^[\w.-]+-unavailable$")
DELEGATION_SOURCES = ("route_weights", "static", "unknown")
REASON_KEPT_CHARS = 300


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


def _number(value: object) -> float | None:
    """keepalive_state.js `hasFiniteNumericValue`, returning the number it found: null, blank strings
    and anything non-finite are not numbers."""
    if not isinstance(value, (int, float, str)) or (isinstance(value, str) and not value.strip()):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _finite(value: object) -> bool:
    return _number(value) is not None


def _int_or_none(value: object) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def is_loop_state(data: object) -> bool:
    """keepalive_state.js `isLoopState`, ported clause for clause. Keepalive reads the newest trusted
    marker that is a LOOP state and falls back to the newest trusted marker of any kind, so this
    predicate decides which marker a PR ended with."""
    if not isinstance(data, dict):
        return False
    if _finite(data.get("iteration")) or _finite(data.get("max_iterations")):
        return True
    tasks = data.get("tasks")
    if isinstance(tasks, dict) and (_finite(tasks.get("total")) or _finite(tasks.get("unchecked"))):
        return True
    if any(key in data for key in ("keepalive_enabled", "autofix_enabled", "running")):
        return True
    if any(isinstance(data.get(key), (dict, list)) for key in ("verification", "last_instruction")):
        return True
    if isinstance(data.get("current_agent"), str) and data["current_agent"]:
        return True
    return any(
        isinstance(data.get(key), list) and bool(data[key])
        for key in ("delegation_log", "effectiveness_history")
    )


def _comment_author(node: dict[str, Any]) -> tuple[str, str]:
    """(kind, login) as keepalive_state.js sees a comment's author. GraphQL names a Bot by its bare
    slug (`stranske-keepalive`) where REST — and therefore keepalive — says `stranske-keepalive[bot]`.
    The suffix is restored for a Bot only, so no User login can acquire it."""
    author = node.get("author") if isinstance(node.get("author"), dict) else {}
    kind = str((author or {}).get("__typename") or "").strip().lower()
    login = str((author or {}).get("login") or "").strip().lower()
    if kind == "bot" and login and not login.endswith("[bot]"):
        login += "[bot]"
    return kind, login


def trusted_state_author(kind: str, login: str) -> bool:
    return (kind == "bot" and login in TRUSTED_STATE_BOTS) or (
        kind == "user" and login in TRUSTED_STATE_USERS
    )


def delegation_source_of(reason: str) -> str:
    """`route_weights` / `static` / `unknown` for one delegation_log entry, from its reason."""
    match = DELEGATION_SOURCE_RE.search(reason or "")
    if match:
        return match.group(1)
    if UNAVAILABLE_REASON_RE.match((reason or "").strip()):
        return "static"
    return "unknown"


def _log_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """A delegation_log entry kept in the facts cache. The source is derived from the FULL reason
    before the reason is shortened for storage, because the suffix it is read from ends the reason.
    """
    reason = str(entry.get("reason") or "")
    return {
        "iteration": _int_or_none(entry.get("iteration")),
        "previous_agent": str(entry.get("previous_agent") or "").strip().lower(),
        "chosen_agent": str(entry.get("chosen_agent") or "").strip().lower(),
        "reason": reason[:REASON_KEPT_CHARS],
        "timestamp": str(entry.get("timestamp") or ""),
        "delegation_source": delegation_source_of(reason),
    }


def select_state(nodes: list[dict[str, Any]], *, older_comments: bool) -> dict[str, Any]:
    """The keepalive state a PR ended with, read from its comment nodes (oldest first) the way
    keepalive_state.js `findStateComment` reads it: newest first, every comment not by a trusted
    writer skipped, the first LOOP state wins, else the newest trusted state of any kind. A comment's
    marker is its FIRST marker (`parseStateComment`), and an unparseable payload is no state at all.

    Returns what the facts cache keeps — who wrote the marker, its switch_count and delegation_log —
    never the payload. `read` is `marker`, `no_marker` (every comment read, none trusted), `truncated`
    (no trusted loop state in the newest page and older comments exist) or `unparseable`; only the
    first two are measurements."""
    untrusted = 0
    chosen: tuple[str, dict[str, Any] | None] | None = None
    fallback: tuple[str, dict[str, Any] | None] | None = None
    for node in reversed(nodes):
        match = keepalive_shadow.STATE_REGEX.search(str(node.get("body") or ""))
        if not match:
            continue
        kind, login = _comment_author(node)
        if not trusted_state_author(kind, login):
            untrusted += 1
            continue
        if chosen is not None:
            continue
        try:
            parsed = json.loads(match.group(1))
        except ValueError:
            parsed = None
        payload = parsed if isinstance(parsed, dict) else None
        if payload is not None and is_loop_state(payload):
            chosen = (login, payload)
        elif fallback is None:
            fallback = (login, payload)
    base = {"untrusted_markers": untrusted, "comments_read": len(nodes)}
    if chosen is None and older_comments:
        return {"read": "truncated", **base}
    picked = chosen or fallback
    if picked is None:
        return {"read": "no_marker", **base}
    author, payload = picked
    if payload is None:
        return {"read": "unparseable", "author": author, **base}
    log = payload.get("delegation_log")
    return {
        "read": "marker",
        "author": author,
        "switch_count": _int_or_none(payload.get("switch_count")),
        "delegation_log": (
            [_log_entry(e) for e in log if isinstance(e, dict)] if isinstance(log, list) else []
        ),
        **base,
    }


def _state_from_graphql(pr: dict[str, Any]) -> dict[str, Any]:
    comments = pr.get("comments")
    if not isinstance(comments, dict):
        return {"read": "failed", "attempts": 1}
    page = comments.get("pageInfo") if isinstance(comments.get("pageInfo"), dict) else {}
    nodes = [n for n in comments.get("nodes") or [] if isinstance(n, dict)]
    return select_state(nodes, older_comments=bool((page or {}).get("hasPreviousPage")))


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
    fact = {
        "label_events": events,
        "commit_ts": sorted(commits),
        "state": str(pr.get("state") or ""),
        "merged_ts": _epoch(pr.get("mergedAt")),
        "closed_ts": _epoch(pr.get("closedAt")),
    }
    if "comments" in pr:
        fact["keepalive_state"] = _state_from_graphql(pr)
    return fact


def _pr_selection(number: int, *, with_state: bool) -> str:
    """One PR's selection. Without the state it is the query this module ran before 2026-09-23."""
    return (
        f"p{number}: pullRequest(number:{number}){{ number state mergedAt closedAt "
        f"timelineItems(itemTypes:[LABELED_EVENT,UNLABELED_EVENT], first:80){{ nodes {{ "
        f"__typename ... on LabeledEvent {{ createdAt label{{name}} }} "
        f"... on UnlabeledEvent {{ createdAt label{{name}} }} }} }} "
        f"commits(first:100){{ nodes {{ commit {{ committedDate }} }} }}"
        f"{STATE_SELECTION if with_state else ''} }}"
    )


def _repository_query(
    repo: str, fields: str, gh_json: Callable[[list[str]], object | None]
) -> dict[int, dict[str, Any]]:
    """The PR nodes one repository query returned, by number; {} when the query failed outright."""
    owner, _, name = repo.partition("/")
    query = f'query {{ repository(owner:"{owner}", name:"{name}") {{ {fields} }} }}'
    data = gh_json(["gh", "api", "graphql", "-f", f"query={query}"])
    payload = data.get("data") if isinstance(data, dict) else None
    repo_data = payload.get("repository") if isinstance(payload, dict) else None
    if not isinstance(repo_data, dict):
        return {}
    return {
        int(pr["number"]): pr
        for pr in repo_data.values()
        if isinstance(pr, dict) and pr.get("number") is not None
    }


def fetch_facts(
    repo: str,
    numbers: list[int],
    *,
    gh_json: Callable[[list[str]], object | None] = _gh_json,
    with_state: bool = True,
) -> dict[int, dict[str, Any]]:
    """Label timeline, commit dates and the keepalive state, one GraphQL query per 25 PRs. A PR the
    chunk did not return is retried on its own, so a deleted PR costs only itself.

    The comments connection is the newest and heaviest part of the query, and an error inside it
    nulls the whole PR node. A PR whose comments cannot be read must not lose its label timeline to
    them, so a PR that fails alone is re-read label-only — exactly the query this module ran before
    — with its state marked `failed`, which the backfill retries."""
    out: dict[int, dict[str, Any]] = {}
    for i in range(0, len(numbers), GRAPHQL_CHUNK):
        chunk = numbers[i : i + GRAPHQL_CHUNK]
        got = _repository_query(
            repo, " ".join(_pr_selection(n, with_state=with_state) for n in chunk), gh_json
        )
        for n in chunk:
            if n in got:
                out[n] = _fact_from_graphql(got[n])
            elif len(chunk) > 1:
                out.update(fetch_facts(repo, [n], gh_json=gh_json, with_state=with_state))
            elif with_state:
                for m, fact in fetch_facts(repo, [n], gh_json=gh_json, with_state=False).items():
                    out[m] = {**fact, "keepalive_state": {"read": "failed", "attempts": 1}}
    return out


def fetch_states(
    repo: str,
    numbers: list[int],
    *,
    gh_json: Callable[[list[str]], object | None] = _gh_json,
) -> dict[int, dict[str, Any]]:
    """The keepalive state alone, for a fact cached before this module read it: one GraphQL query per
    25 PRs over the same comments selection as the facts query. The cached label timeline is never
    re-read, so a backfill cannot move a label-derived row. A PR that cannot be read reads `failed`.
    """
    out: dict[int, dict[str, Any]] = {}
    for i in range(0, len(numbers), GRAPHQL_CHUNK):
        chunk = numbers[i : i + GRAPHQL_CHUNK]
        fields = " ".join(
            f"p{n}: pullRequest(number:{n}){{ number{STATE_SELECTION} }}" for n in chunk
        )
        got = _repository_query(repo, fields, gh_json)
        for n in chunk:
            if n in got:
                out[n] = _state_from_graphql(got[n])
            elif len(chunk) > 1:
                out.update(fetch_states(repo, [n], gh_json=gh_json))
            else:
                out[n] = {"read": "failed"}
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


def derive_policy_switches(fact: dict[str, Any]) -> dict[str, Any]:
    """Each entry of the PR's trusted delegation_log as a switch: previous_agent -> chosen_agent at the
    entry's timestamp, carrying that entry's delegation_source. Commits split at the timestamp as the
    label path's do — but keepalive writes the entry when it saves the state AFTER the chosen agent's
    first round (keepalive_loop.js updateKeepaliveLoopSummary), so that round's commit counts as
    before. An entry with no chosen agent or no readable timestamp is counted, never recorded."""
    raw = fact.get("keepalive_state")
    state: dict[str, Any] = raw if isinstance(raw, dict) else {}
    if state.get("read") != "marker":
        return {"switches": [], "malformed": 0}
    commits = [int(t) for t in fact.get("commit_ts") or []]
    switches: list[dict[str, Any]] = []
    malformed = 0
    for entry in state.get("delegation_log") or []:
        if not isinstance(entry, dict):
            malformed += 1
            continue
        to_agent = str(entry.get("chosen_agent") or "").strip().lower()
        ts = _epoch(entry.get("timestamp"))
        if not to_agent or ts is None:
            malformed += 1
            continue
        source = entry.get("delegation_source")
        if source not in DELEGATION_SOURCES:
            source = delegation_source_of(str(entry.get("reason") or ""))
        switches.append(
            {
                "from_agent": str(entry.get("previous_agent") or "").strip().lower() or "unknown",
                "to_agent": to_agent,
                "switched_ts": ts,
                "iteration": entry.get("iteration"),
                "delegation_source": source,
                "commits_before": sum(1 for c in commits if c < ts),
                "commits_after": sum(1 for c in commits if c >= ts),
            }
        )
    return {"switches": switches, "malformed": malformed}


INSERT_SWITCH = (
    "INSERT OR REPLACE INTO agent_switches (pr_ref, from_agent, to_agent, switched_ts, "
    "auto_label, commits_before, commits_after, merged, durability, recorded_ts, source, "
    "delegation_source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _record(pr: dict, derived: dict[str, Any], *, now: int) -> None:
    """Label rows as they always were, with `source='label'`; policy rows beside them. `source` is in
    the key, so neither kind can replace the other."""
    rows = [(sw, SOURCE_LABEL, None) for sw in derived["switches"]] + [
        (sw, SOURCE_POLICY, sw["delegation_source"]) for sw in derived.get("policy_switches") or []
    ]
    with feedback._conn() as c:
        for sw, source, delegation_source in rows:
            c.execute(
                INSERT_SWITCH,
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
                    source,
                    delegation_source,
                ),
            )


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def _tally(
    cells: dict[str, dict[str, Any]],
    sw: dict[str, Any],
    pr: dict,
    *,
    auto_label: bool,
    delegation_source: str | None = None,
) -> None:
    key = f"{sw['from_agent']}->{sw['to_agent']}"
    cell = cells.setdefault(
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
    cell["auto_label"] += 1 if auto_label else 0
    cell["commits_before"].append(float(sw["commits_before"]))
    cell["commits_after"].append(float(sw["commits_after"]))
    if len(cell["examples"]) < 5:
        cell["examples"].append(pr["ref"])
    if delegation_source is not None:
        for source in DELEGATION_SOURCES:
            cell.setdefault(source, 0)
        cell[delegation_source] += 1


def _rows(cells: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        key: {
            **{k: v for k, v in cell.items() if k not in ("commits_before", "commits_after")},
            "commits_before_median": _median(cell["commits_before"]),
            "commits_after_median": _median(cell["commits_after"]),
        }
        for key, cell in sorted(cells.items(), key=lambda kv: (-kv[1]["n"], kv[0]))
    }


def aggregate(
    prs: list[dict], facts: dict[str, dict[str, Any]], *, now: int, window_days: int
) -> tuple[dict[str, Any], list[tuple[dict, dict[str, Any]]]]:
    missing: list[str] = []
    with_facts = switched_prs = auto_labeled = auto_and_switched = 0
    policy_switched_prs = policy_malformed = log_truncated = untrusted = 0
    transitions: dict[str, dict[str, Any]] = {}
    policy_transitions: dict[str, dict[str, Any]] = {}
    by_source = dict.fromkeys(DELEGATION_SOURCES, 0)
    reads: dict[str, int] = {}
    recordable: list[tuple[dict, dict[str, Any]]] = []
    for pr in prs:
        fact = facts.get(pr["ref"])
        if not fact or fact.get("unavailable"):
            missing.append(pr["ref"])
            continue
        with_facts += 1
        derived = derive_switches(fact)
        policy = derive_policy_switches(fact)
        derived["policy_switches"] = policy["switches"]
        auto_labeled += 1 if derived["auto_label"] else 0
        state = (
            fact.get("keepalive_state") if isinstance(fact.get("keepalive_state"), dict) else None
        )
        read = str(state.get("read")) if state else "not_yet_read"
        reads[read] = reads.get(read, 0) + 1
        if state:
            untrusted += int(state.get("untrusted_markers") or 0)
            count = state.get("switch_count")
            logged = len(state.get("delegation_log") or [])
            # keepalive keeps only the last 10 entries, so a PR past that has switches no log holds
            if read == "marker" and isinstance(count, int) and count > logged:
                log_truncated += 1
        policy_malformed += policy["malformed"]
        if policy["switches"]:
            policy_switched_prs += 1
            for sw in policy["switches"]:
                by_source[sw["delegation_source"]] += 1
                _tally(
                    policy_transitions,
                    sw,
                    pr,
                    auto_label=derived["auto_label"],
                    delegation_source=sw["delegation_source"],
                )
        if derived["switches"] or policy["switches"]:
            recordable.append((pr, derived))
        if not derived["switches"]:
            continue
        switched_prs += 1
        auto_and_switched += 1 if derived["auto_label"] else 0
        for sw in derived["switches"]:
            _tally(transitions, sw, pr, auto_label=derived["auto_label"])
    state_read = sum(reads.get(value, 0) for value in STATE_READ_MEASURED)
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
            "policy_switched_prs": policy_switched_prs,
            "policy_switches": sum(by_source.values()),
            "policy_route_weights": by_source["route_weights"],
            "policy_static": by_source["static"],
            "policy_source_unknown": by_source["unknown"],
            "policy_entries_malformed": policy_malformed,
            "policy_log_truncated": log_truncated,
            "state_read": state_read,
            "state_marker": reads.get("marker", 0),
            "state_no_marker": reads.get("no_marker", 0),
            "state_unread": with_facts - state_read,
            "untrusted_markers_ignored": untrusted,
        },
        "missing_facts": missing[:MISSING_REPORTED_MAX],
        "transitions": _rows(transitions),
        "policy_transitions": _rows(policy_transitions),
        "state_unread_reasons": {
            value: n for value, n in sorted(reads.items()) if value not in STATE_READ_MEASURED
        },
        "definitions": {
            "switch": "consecutive distinct real agent labels applied to one PR; agent:auto and "
            "agent:rate-limited are not agents",
            "commits_before_after": "commit dates against the arriving label's timestamp",
            "excluded": "bot and owner rows (attribution_source bot:*/human)",
            "policy": "agent_delegation_policy.js switches on agent:auto after two rounds without "
            "progress, five-round cooldown",
            "policy_switch": "one delegation_log entry in the PR's latest keepalive state marker from "
            "a trusted writer (keepalive_state.js isTrustedKeepaliveStateComment); the policy records "
            "its switches only there and never relabels",
            "delegation_source": "per entry, from the reason suffix 'delegation_source: "
            "route_weights|static' the policy writes; '<agent>-unavailable' switches are static by "
            "construction; any other reason is unknown. The state's top-level delegation_source is "
            "the latest round's, not any switch's, and is not used",
            "commits_before_after_policy": "commit dates against the entry timestamp, which keepalive "
            "writes after the chosen agent's first round, so that round's commit counts as before",
            "state_read": "PRs whose keepalive state was measured: a trusted marker read, or every "
            "comment read and none carried one. Unread PRs are never counted as zero switches; "
            "state_unread_reasons says why each is unread",
        },
    }
    return payload, recordable


def _state_needs_read(fact: dict[str, Any]) -> bool:
    state = fact.get("keepalive_state")
    if not isinstance(state, dict):
        return True  # cached before the state was read
    return state.get("read") == "failed" and int(state.get("attempts") or 1) < STATE_READ_ATTEMPTS


def _backfill_states(
    prs: list[dict],
    facts: dict[str, dict[str, Any]],
    *,
    budget: int,
    fetch: Callable[[str, list[int]], dict[int, dict[str, Any]]],
) -> int:
    """Read the keepalive state of every cached fact that lacks one, or whose comments failed to read
    fewer than STATE_READ_ATTEMPTS times, within `budget`. Only `keepalive_state` is written."""
    stale_by_repo: dict[str, list[int]] = {}
    for pr in prs:
        fact = facts.get(pr["ref"])
        if fact and not fact.get("unavailable") and _state_needs_read(fact):
            stale_by_repo.setdefault(pr["repo"], []).append(pr["number"])
    read = 0
    for repo, numbers in sorted(stale_by_repo.items()):
        if budget <= 0:
            break
        take = numbers[:budget]
        budget -= len(take)
        got = fetch(repo, take)
        for number in take:
            fact = facts[f"{repo}#{number}"]
            prior = (
                fact.get("keepalive_state") if isinstance(fact.get("keepalive_state"), dict) else {}
            )
            state = got.get(number) or {"read": "failed"}
            if state.get("read") == "failed":
                state = {**state, "attempts": int((prior or {}).get("attempts") or 0) + 1}
            fact["keepalive_state"] = state
            read += 1
    return read


def run(
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    state_dir: Path | None = None,
    fetch_limit: int = FETCH_LIMIT_DEFAULT,
    now: int | None = None,
    fetch_fn: Callable[[str, list[int]], dict[int, dict[str, Any]]] | None = None,
    state_fetch_fn: Callable[[str, list[int]], dict[int, dict[str, Any]]] | None = None,
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
    # What the budget has left reads the keepalive state of facts cached before it was read. New
    # facts go first: they carry their state already, and they are the label measurement.
    backfilled = _backfill_states(prs, facts, budget=budget, fetch=state_fetch_fn or fetch_states)
    if fetched or unavailable or backfilled:
        save_facts(state_dir, facts, now=now)
    payload, recordable = aggregate(prs, facts, now=now, window_days=window_days)
    for pr, derived in recordable:
        _record(pr, derived, now=now)
    payload["counts"]["fetched_this_run"] = fetched
    payload["counts"]["unavailable_this_run"] = unavailable
    payload["counts"]["state_backfilled_this_run"] = backfilled
    payload["counts"]["recorded_in_brain"] = sum(
        len(d["switches"]) + len(d["policy_switches"]) for _, d in recordable
    )
    payload["counts"]["recorded_policy"] = sum(len(d["policy_switches"]) for _, d in recordable)
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


# ---------------------------------------------------------------- report -------------------------


def summary_for_report(state_dir: Path | None = None) -> dict[str, Any]:
    path = (state_dir or default_state_dir()) / "agent-switches.json"
    payload = load_state(state_dir or default_state_dir())
    if not payload or "counts" not in payload:
        return {"state": "not yet recorded", "artifact": str(path)}
    return {
        "state": "recorded",
        "generated_at": payload.get("generated_at"),
        "window_days": payload.get("window_days"),
        "counts": payload.get("counts") or {},
        "transitions": {
            k: {"n": v.get("n"), "merged": v.get("merged"), "bad": v.get("bad")}
            for k, v in (payload.get("transitions") or {}).items()
        },
        "policy_transitions": {
            k: {
                "n": v.get("n"),
                "merged": v.get("merged"),
                "bad": v.get("bad"),
                "route_weights": v.get("route_weights", 0),
            }
            for k, v in (payload.get("policy_transitions") or {}).items()
        },
        "state_unread_reasons": payload.get("state_unread_reasons") or {},
        "sample": {"retired": SAMPLE_RETIRED},
    }


def policy_phrase(summary: dict[str, Any]) -> str:
    """The policy half of both the report headline and the tick's SWITCHES line — one phrase, so the
    two surfaces cannot disagree. Zero switches is only ever printed beside the number of PRs whose
    state was READ; an artifact written before the state was read says so instead of printing 0."""
    c = summary.get("counts") or {}
    if "state_read" not in c:
        return "policy switches not measured (this artifact predates the keepalive-state read)"
    reasons = ", ".join(
        f"{why} {n}" for why, n in sorted((summary.get("state_unread_reasons") or {}).items()) if n
    )
    # Each of these makes the policy count an UNDERCOUNT, so each is printed whenever it is not zero.
    # Untrusted markers most of all: a new keepalive writer missing from TRUSTED_STATE_* would have
    # its markers ignored here, and the line would otherwise read as a quiet zero.
    caveats = [
        f"{c[key]} {what}"
        for key, what in (
            ("untrusted_markers_ignored", "markers from untrusted authors ignored"),
            ("policy_entries_malformed", "malformed log entries"),
            ("policy_log_truncated", "logs past keepalive's 10-entry cap"),
        )
        if c.get(key)
    ]
    return (
        f"policy switches {c.get('policy_switches')} on {c.get('policy_switched_prs')} PRs, "
        f"{c.get('policy_route_weights')} via route_weights ({c.get('policy_static')} static, "
        f"{c.get('policy_source_unknown')} unknown), from the keepalive state of "
        f"{c.get('state_read')} PRs ({c.get('state_unread')} unread"
        f"{': ' + reasons if reasons else ''})" + "".join(f"; {caveat}" for caveat in caveats)
    )


def render_report_lines(summary: dict[str, Any]) -> list[str]:
    if summary.get("state") != "recorded":
        return [
            f"AGENT-SWITCHES: {summary.get('state', 'unknown')} ({summary.get('artifact', '')})"
        ]
    c = summary.get("counts") or {}
    lines = [
        f"AGENT-SWITCHES ({summary.get('window_days')}d): {c.get('switched_prs')} of "
        f"{c.get('with_facts')} keepalive PRs with facts switched agents by label "
        f"({c.get('switches')} label switches; {c.get('missing_facts')} facts missing); "
        f"{policy_phrase(summary)}; agent:auto on {c.get('auto_labeled')}, of which "
        f"{c.get('auto_and_switched')} switched by label; sampling retired {SAMPLE_RETIRED} "
        "(the opener labels agent:auto at creation)"
    ]
    for key, cell in (summary.get("transitions") or {}).items():
        lines.append(
            f"  {key}: {cell.get('n')} (merged {cell.get('merged')}, bad {cell.get('bad')})"
        )
    for key, cell in (summary.get("policy_transitions") or {}).items():
        lines.append(
            f"  policy {key}: {cell.get('n')} ({cell.get('route_weights')} via route_weights, "
            f"merged {cell.get('merged')}, bad {cell.get('bad')})"
        )
    return lines


def render_tick_line(summary: dict[str, Any]) -> str:
    """The tick's one-line SWITCHES summary, printed by orchestrate.sh after a successful run."""
    if summary.get("state") != "recorded":
        return f"  SWITCHES: {summary.get('state', 'unknown')} ({summary.get('artifact', '')})"
    c = summary.get("counts") or {}
    return (
        f"  SWITCHES: {c.get('switched_prs')} of {c.get('with_facts')} keepalive PRs switched agents "
        f"by label ({c.get('switches')} label switches); {policy_phrase(summary)}; "
        f"{c.get('recorded_in_brain')} recorded; {c.get('missing_facts')} facts missing; "
        f"agent:auto on {c.get('auto_labeled')}, {c.get('auto_and_switched')} of those switched "
        "by label"
    )


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
    check("sample" not in globals(), "the retired sample step stays retired")
    line = render_report_lines({"state": "not yet recorded", "artifact": "x"})[0]
    check(line.startswith("AGENT-SWITCHES: not yet recorded"), "unrecorded line")

    def comment(kind: str, login: str, payload: object) -> dict[str, Any]:
        body = f"summary\n<!-- keepalive-state:v1 {json.dumps(payload)} -->"
        return {"author": {"__typename": kind, "login": login}, "body": body}

    log = [
        {
            "iteration": 5,
            "previous_agent": "codex",
            "chosen_agent": "claude",
            "timestamp": "2026-09-20T10:00:00.123Z",
            "reason": "codex-stalled (x; delegation_source: route_weights)",
        },
        {
            "iteration": 11,
            "previous_agent": "claude",
            "chosen_agent": "cursor",
            "timestamp": "2026-09-21T10:00:00Z",
            "reason": "claude-stalled (x; delegation_source: static (preference-order))",
        },
    ]
    nodes = [
        comment(
            "Bot", "stranske-keepalive", {"iteration": 12, "switch_count": 2, "delegation_log": log}
        ),
        comment("User", "mallory", {"iteration": 13, "delegation_log": log + log}),
    ]
    s = select_state(nodes, older_comments=False)
    check(s["read"] == "marker" and s["author"] == "stranske-keepalive[bot]", f"trusted pick {s}")
    check(
        [e["delegation_source"] for e in s["delegation_log"]] == ["route_weights", "static"], "src"
    )
    check(s["untrusted_markers"] == 1, "the later untrusted marker is counted and ignored")
    spoof = select_state(
        [comment("User", "stranske-keepalive", {"iteration": 1})], older_comments=False
    )
    check(spoof["read"] == "no_marker", "a User login never passes for the keepalive Bot")
    check(
        select_state([], older_comments=True)["read"] == "truncated", "older comments -> truncated"
    )
    check(delegation_source_of("codex-unavailable") == "static", "unavailable switches are static")
    check(
        delegation_source_of("codex-stalled (x)") == "unknown", "no suffix -> unknown, not static"
    )
    old = policy_phrase({"counts": {"switches": 0}})
    check(
        old.startswith("policy switches not measured"), "a pre-read artifact is unmeasured, not 0"
    )
    if failures:
        print("agent_switches.py selftest: FAIL — " + "; ".join(failures))
        return 1
    print(
        "agent_switches.py selftest: OK (switch derivation ignores auto/rate-limited and collapses "
        "repeats, commits split at the arriving label, unrecorded line named, sample step retired; "
        "policy switches read from the latest TRUSTED state marker with per-entry delegation_source, "
        "untrusted and type-spoofed markers ignored, truncated and pre-read states never read as 0)"
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
    show_p = sub.add_parser("show", help="print the current summary")
    show_p.add_argument("--state-dir", type=Path, default=None)
    tick_p = sub.add_parser("tick-line", help="print the tick's one-line SWITCHES summary")
    tick_p.add_argument("--state-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.command == "tick-line":
        print(render_tick_line(summary_for_report(args.state_dir)))
        return 0
    if args.command == "run":
        payload = run(
            window_days=args.window_days, state_dir=args.state_dir, fetch_limit=args.fetch_limit
        )
        if args.json:
            print(json.dumps({k: v for k, v in payload.items() if k != "sample"}, indent=1))
        else:
            print("\n".join(render_report_lines(summary_for_report(args.state_dir))))
        return 0
    print("\n".join(render_report_lines(summary_for_report(args.state_dir))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
