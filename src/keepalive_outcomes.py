#!/usr/bin/env python3
"""Ingest keepalive-driven PR outcomes into the feedback Brain.

This complements outcomes.py: orchestrator-delegated remote runs already have
run rows. Keepalive can also drive agent PRs without an orchestrator decision,
so this records source-tagged, decision-light evidence under stable run ids.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, TypedDict, cast

import durability_sweep
import feedback
import outcomes

# Source-of-truth registry lives in Dropbox, but launchd/cron CANNOT read CloudStorage
# paths (EPERM "Operation not permitted") — so under the scheduled tick we must read a
# LOCAL-DISK copy that orch-sync-mirror.sh places next to the mirror. Resolution order:
# env override -> mirror copy -> runtime copy -> Dropbox source (interactive fallback).
DROPBOX_REGISTRY_PATH = (
    Path.home()
    / "Library/CloudStorage/Dropbox/Learning/Code/Workflows/config/repo_review_registry.json"
)
_LOCAL_REGISTRY_CANDIDATES = (
    Path.home() / ".codex/orchestrator-mirror/repo_review_registry.json",
    Path.home() / ".codex/orchestrator/repo_review_registry.json",
)
# Back-compat module-level default (resolution happens lazily in _active_repos).
REGISTRY_PATH = DROPBOX_REGISTRY_PATH


def _resolve_registry_path() -> Path:
    """Pick the first readable registry, preferring local-disk copies (launchd-safe)."""
    env = os.environ.get("ORCH_REPO_REVIEW_REGISTRY")
    candidates = ([Path(env).expanduser()] if env else []) + [
        *_LOCAL_REGISTRY_CANDIDATES,
        DROPBOX_REGISTRY_PATH,
    ]
    for cand in candidates:
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue  # EPERM probing a CloudStorage path under launchd — try the next
    return DROPBOX_REGISTRY_PATH


PR_LIST_FIELDS = (
    "number,state,title,labels,createdAt,updatedAt,mergedAt,closedAt,"
    "headRefName,baseRefName,mergeCommit,author,body,url,commits"
)
PR_CONTEXT_FIELDS = "body,comments"
PROCESS_WORK_TYPES = {"renovate", "sync", "tooling", "docs"}
PROCESS_IGNORE_DUPLICATE_OR_SUPERSEDED = "duplicate_or_superseded"
PROCESS_IGNORE_MARKER = f"process_ignore={PROCESS_IGNORE_DUPLICATE_OR_SUPERSEDED}"
PROCESS_IGNORE_RE = re.compile(r"\bprocess_ignore=([a-z0-9_:-]+)\b", re.IGNORECASE)
PROCESS_DUPLICATE_CLOSURE_RE = re.compile(
    r"\b("
    r"closing as (?:a )?duplicate|"
    r"closed as (?:a )?duplicate|"
    r"duplicate opener|"
    r"superseded(?: by)?|"
    r"already materialized|"
    r"canonical opener|"
    r"concurrent(?:ly)? for the same source issue|"
    r"keeping #\d+ as (?:the )?(?:active|canonical)"
    r")\b",
    re.IGNORECASE,
)


def _active_repos(registry_path: Path | None = None) -> list[str]:
    path = registry_path or _resolve_registry_path()
    try:
        data = json.loads(path.expanduser().read_text())
    except OSError as exc:
        raise RuntimeError(
            f"repo_review_registry unreadable at {path} ({exc}); under launchd this is "
            "usually EPERM on a CloudStorage path — run orch-sync-mirror.sh to place a "
            "local copy (~/.codex/orchestrator-mirror/repo_review_registry.json)"
        ) from exc
    repos = []
    for item in data.get("repos", []):
        if item.get("status") == "active" and item.get("repo"):
            repos.append(item["repo"])
    return repos


def _run_json(args: list[str], *, timeout: int = 30) -> object | None:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


def _since_date(lookback_days: int, now: int | None = None) -> str:
    now = int(now or time.time())
    dt = _dt.datetime.fromtimestamp(now - lookback_days * 86400, _dt.timezone.utc)
    return dt.strftime("%Y-%m-%d")


def _fetch_prs(repo: str, lookback_days: int, *, now: int | None = None) -> list[dict]:
    """Live GitHub fetch: broad PR metadata, filtered locally by agent labels."""
    since = _since_date(lookback_days, now=now)
    arr = _run_json(
        [
            "gh",
            "pr",
            "list",
            "-R",
            repo,
            "--state",
            "all",
            "--search",
            f"updated:>={since}",
            "--limit",
            "300",
            "--json",
            PR_LIST_FIELDS,
        ]
    )
    return arr if isinstance(arr, list) else []


def _fetch_pr_context(repo: str, pr_number: int) -> str:
    obj = _run_json(
        ["gh", "pr", "view", str(pr_number), "--repo", repo, "--json", PR_CONTEXT_FIELDS]
    )
    if not isinstance(obj, dict):
        return ""
    parts = []
    if obj.get("body"):
        parts.append(str(obj["body"]))
    comments = obj.get("comments") or []
    if isinstance(comments, list):
        for comment in comments:
            if isinstance(comment, dict) and comment.get("body"):
                parts.append(str(comment["body"]))
            elif isinstance(comment, str):
                parts.append(comment)
    return "\n".join(parts)


def process_suppression_reason(text: str | None) -> str | None:
    haystack = text or ""
    marker = PROCESS_IGNORE_RE.search(haystack)
    if marker:
        return marker.group(1).lower()
    if PROCESS_DUPLICATE_CLOSURE_RE.search(haystack):
        return PROCESS_IGNORE_DUPLICATE_OR_SUPERSEDED
    return None


def _with_process_ignore_note(oc: dict, reason: str) -> dict:
    marker = f"process_ignore={reason}"
    notes = oc.get("notes") or ""
    if marker in notes:
        return oc
    tagged = dict(oc)
    tagged["notes"] = f"{notes}; {marker}" if notes else marker
    return tagged


def _maybe_mark_process_ignore(
    repo: str,
    pr: dict,
    work_type: str,
    oc: dict | None,
    closure_context_fn,
) -> dict | None:
    if (
        oc is None
        or oc.get("durability") != "abandoned"
        or work_type not in PROCESS_WORK_TYPES
        or str(pr.get("state") or "").upper() != "CLOSED"
        or pr.get("number") is None
    ):
        return oc
    try:
        pr_number = int(pr["number"])
    except (TypeError, ValueError):
        return oc
    _gh_throttle("core")
    reason = process_suppression_reason(closure_context_fn(repo, pr_number))
    if not reason:
        return oc
    return _with_process_ignore_note(oc, reason)


def _label_names(pr: dict) -> list[str]:
    names = []
    for item in pr.get("labels") or []:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
    return names


def _author_login(pr: dict) -> str | None:
    author = pr.get("author")
    if isinstance(author, dict) and author.get("login"):
        return str(author["login"])
    if isinstance(author, str):
        return author
    return None


# Real implementer agents only. `agent:auto` (delegation-policy auto-switch), `agent:rate-limited`, and any
# other `agent:*` CONTROL labels are NOT agents — counting them would pollute the learner with bogus agents.
KNOWN_AGENTS = {"codex", "claude", "cursor", "gemini", "vibe", "aider", "copilot"}
NON_AGENT = "none"
ATTRIBUTION_UNRESOLVED = "unresolved"
AUTHOR_AGENT_MAP = {
    "chatgpt-codex-connector": "codex",
    "chatgpt-codex-connector[bot]": "codex",
    "cursor": "cursor",
    "cursor[bot]": "cursor",
    "copilot": "copilot",
    "copilot[bot]": "copilot",
}
SUMMARY_AGENT_RE = re.compile(
    r"(?:^|\n)\s*(?:[-*]\s*)?(?:\*{1,2})?agent(?:\s+type)?(?:\*{1,2})?\s*:\s*"
    r"(?:\*{1,2})?(codex|claude|cursor|gemini|vibe|aider|copilot)(?:\*{1,2})?\b",
    re.IGNORECASE,
)


# Evidence that names the implementer once the keepalive labels are gone. The lanes open PRs from
# the owner's own GitHub identity and never label them `agent:*`, so on 2026-09-15 3,576 keepalive
# rows sat at agent=none — 567 of them merged since 08-15 — while the branch name, the commit
# identity, or THIS MACHINE'S OWN DISPATCH ROWS named the agent nearly every time. Measured on those
# 567: 48% real agents, 48% bots (template sync, release-please, deps sync, renovate), 3% the owner's
# own hand-made PRs, 3% unresolved. Explicit fleet labels still win; these come after them.
BRANCH_AGENT_RE = re.compile(r"^(codex|claude|cursor|gemini|vibe|aider|copilot)/", re.IGNORECASE)
LANE_ISSUE_BRANCH_RE = re.compile(r"^orchestrator/issue-(\d+)", re.IGNORECASE)
# Non-agent PR classes. The agent column stays NON_AGENT, exactly as for an unresolved row, but the
# attribution_source NAMES the class, so a consumer can exclude bots and the owner's own work from
# agent metrics instead of averaging them into an "unattributed" bucket (that bucket was 517 of 774
# merged outcomes since 08-15, and it mixed Maint 71 deliveries, release-please, and the owner).
BOT_BRANCH_CLASSES = (
    ("sync/workflows", "bot:template-sync"),
    ("renovate/", "bot:renovate"),
    ("release-please", "bot:release"),
    ("deps/", "bot:deps-sync"),
    ("verifier-corpus-harvest/", "bot:verifier-corpus"),
)
BOT_AUTHOR_CLASSES = {
    "renovate[bot]": "bot:renovate",
    "renovate": "bot:renovate",
    "agents-workflows-bot": "bot:release",
    "agents-workflows-bot[bot]": "bot:release",
    "app/agents-workflows-bot": "bot:release",
}
# Substrings of a lowercased commit author name / login / email or a Co-authored-by trailer. Cursor
# commits as `cursoragent <cursoragent@cursor.com>`, Claude Code as author `claude` or with a
# `Co-Authored-By: Claude ...` trailer; codex commits under the owner's identity and leaves no mark,
# so codex resolves through labels, the branch prefix, or the local dispatch join instead.
COMMIT_IDENTITY_AGENTS = (
    ("cursor", "cursor"),
    ("claude", "claude"),
    ("codex", "codex"),
    ("gemini", "gemini"),
    ("aider", "aider"),
)
HUMAN_COMMIT_IDENTITIES = {"tim stranske"}
ATTRIBUTION_HUMAN = "human"


class IngestSummary(TypedDict):
    repos: int
    prs_seen: int
    runs_recorded: int
    outcomes_recorded: int
    skipped_existing: int
    non_agent_prs_seen: int
    non_agent_runs_recorded: int
    by_source: dict[str, int]
    attribution: dict[str, object]


def _agent_from_labels(labels: list[str]) -> tuple[str, str] | None:
    agents = []
    for label in labels:
        if label.strip().lower().startswith("agent:"):
            agent = label.split(":", 1)[1].strip()
            if agent.lower() in KNOWN_AGENTS and agent.lower() not in agents:
                agents.append(agent.lower())
    return (agents[0], "agent_label") if len(agents) == 1 else None


def _agent_from_tried_labels(labels: list[str]) -> tuple[str, str] | None:
    agents = []
    for label in labels:
        low = label.strip().lower()
        if low.startswith("agents:tried-"):
            agent = low.removeprefix("agents:tried-").strip()
            if agent in KNOWN_AGENTS and agent not in agents:
                agents.append(agent)
    return (agents[0], "tried_label") if len(agents) == 1 else None


def _agent_from_job_names(job_names: list[str]) -> tuple[str, str] | None:
    agents = []
    for name in job_names:
        low = str(name).strip().lower()
        for agent in KNOWN_AGENTS:
            if low == f"run-{agent}" or re.search(
                rf"\bkeepalive\s+next\s+task\s*\(\s*{re.escape(agent)}\s*\)", low
            ):
                if agent not in agents:
                    agents.append(agent)
    return (agents[0], "keepalive_job") if len(agents) == 1 else None


def _agent_from_author(author: str | None) -> tuple[str, str] | None:
    agent = AUTHOR_AGENT_MAP.get((author or "").strip().lower())
    return (agent, "author_login") if agent else None


def _agent_from_summary(summary: str | None) -> tuple[str, str] | None:
    if "automated status summary" not in (summary or "").lower():
        return None
    match = SUMMARY_AGENT_RE.search((summary or "").replace("*", ""))
    return (match.group(1).lower(), "automated_status_summary") if match else None


def _agent_from_branch(head_ref: str | None) -> tuple[str, str] | None:
    match = BRANCH_AGENT_RE.match((head_ref or "").strip())
    return (match.group(1).lower(), "branch_prefix") if match else None


def _bot_class(head_ref: str | None, author: str | None) -> tuple[str, str] | None:
    branch = (head_ref or "").strip().lower()
    for prefix, cls in BOT_BRANCH_CLASSES:
        if branch.startswith(prefix):
            return NON_AGENT, cls
    cls = BOT_AUTHOR_CLASSES.get((author or "").strip().lower())
    return (NON_AGENT, cls) if cls else None


def _agent_from_commit_identities(identities: list[str] | None) -> tuple[str, str] | None:
    ids = {str(i).strip().lower() for i in (identities or []) if i}
    found: list[str] = []
    for needle, agent in COMMIT_IDENTITY_AGENTS:
        if any(needle in i for i in ids) and agent not in found:
            found.append(agent)
    return (found[0], "commit_identity") if len(found) == 1 else None


def _agent_from_local_dispatch(
    repo: str | None, head_ref: str | None, created_ts: int | None
) -> tuple[str, str] | None:
    """The opener lane names its branch `orchestrator/issue-N`, and this machine's Brain already
    holds the dispatch row for repo#N with the agent that ran it. Joined on the issue number, bounded
    to dispatches made before the PR existed (plus a day of clock slack), newest first."""
    match = LANE_ISSUE_BRANCH_RE.match((head_ref or "").strip())
    if not (match and repo):
        return None
    limit = int(created_ts or time.time()) + 86400
    with feedback._conn() as c:
        row = c.execute(
            "SELECT agent FROM runs WHERE target=? AND agent!=? AND source!='keepalive' "
            "AND ts<=? ORDER BY ts DESC LIMIT 1",
            (f"{repo}#{match.group(1)}", NON_AGENT, limit),
        ).fetchone()
    if row and str(row[0]).strip().lower() in KNOWN_AGENTS:
        return str(row[0]).strip().lower(), "local_dispatch_join"
    return None


def _human_class(identities: list[str] | None) -> tuple[str, str] | None:
    """The owner's own hand-made PRs: every commit identity is the owner's, and at least one is the
    human git identity rather than the login the lanes and codex commit under."""
    # Names and logins only: an email or a Co-authored-by trailer says who, never that it was by hand.
    ids = {
        str(i).strip().lower()
        for i in (identities or [])
        if i and "@" not in str(i) and "o-authored-by" not in str(i).lower()
    }
    if ids & HUMAN_COMMIT_IDENTITIES and ids <= HUMAN_COMMIT_IDENTITIES | {"stranske"}:
        return NON_AGENT, ATTRIBUTION_HUMAN
    return None


def _commit_identities(pr: dict) -> list[str]:
    """Author names, logins, emails and Co-authored-by trailers from either `gh pr list --json
    commits` (authors[] + messageBody) or a GraphQL `commits.nodes[].commit` shape."""
    out: list[str] = []
    commits = pr.get("commits")
    nodes = commits.get("nodes") if isinstance(commits, dict) else commits
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        commit = node.get("commit") if isinstance(node.get("commit"), dict) else node
        author = commit.get("author")
        authors = (
            author if isinstance(author, list) else [author] if isinstance(author, dict) else []
        )
        for a in commit.get("authors") or authors:
            if isinstance(a, dict):
                user = a.get("user") if isinstance(a.get("user"), dict) else {}
                for key in (a.get("name"), a.get("login"), a.get("email"), user.get("login")):
                    if key:
                        out.append(str(key).lower())
        message = str(commit.get("message") or commit.get("messageBody") or "")
        for line in message.splitlines():
            if "o-authored-by" in line.lower():
                out.append(line.strip().lower())
    return out


def _created_epoch(pr: dict) -> int | None:
    raw = str(pr.get("createdAt") or "").strip()
    if not raw:
        return None
    try:
        return int(
            _dt.datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=_dt.timezone.utc)
            .timestamp()
        )
    except ValueError:
        return None


def derive_attribution(
    labels: list[str],
    *,
    job_names: list[str] | None = None,
    author: str | None = None,
    summary: str | None = None,
    head_ref: str | None = None,
    commit_identities: list[str] | None = None,
    repo: str | None = None,
    created_ts: int | None = None,
) -> tuple[str, str]:
    """Resolve only explicit evidence, in the documented priority order: the fleet's own labels and
    job names, then the PR author, then the branch prefix, then the bot classes, then commit
    identities, then this machine's dispatch rows, then the status summary, then the owner's own
    hand-made PRs. Anything else stays unresolved, never guessed."""
    for resolved in (
        _agent_from_labels(labels),
        _agent_from_tried_labels(labels),
        _agent_from_job_names(job_names or []),
        _agent_from_author(author),
        _agent_from_branch(head_ref),
        _bot_class(head_ref, author),
        _agent_from_commit_identities(commit_identities),
        _agent_from_local_dispatch(repo, head_ref, created_ts),
        _agent_from_summary(summary),
        _human_class(commit_identities),
    ):
        if resolved:
            return resolved
    return NON_AGENT, ATTRIBUTION_UNRESOLVED


def _task_type_from_labels(labels: list[str]) -> str:
    lowered = {lab.lower() for lab in labels}
    for label in labels:
        low = label.lower()
        for prefix in ("task:", "type:"):
            if low.startswith(prefix):
                val = label.split(":", 1)[1].strip()
                if val:
                    return val
    if lowered & {"bug", "fix", "bugfix"}:
        return "fix"
    if lowered & {"docs", "documentation"}:
        return "docs"
    if lowered & {"test", "tests", "testing"}:
        return "test"
    if lowered & {"review", "verification", "verify"}:
        return "review"
    return "implement"


def _work_type(labels: list[str], title: str, author: str | None = None) -> str:
    lowered = {label.strip().lower() for label in labels}
    title_text = title or ""
    title_low = title_text.lower()
    author_low = (author or "").strip().lower()

    if (
        author_low in {"renovate[bot]", "dependabot[bot]", "renovate", "dependabot"}
        or lowered & {"renovate", "dependencies", "dependency", "deps"}
        or re.search(r"\b(?:chore|build)\s*\(\s*deps\s*\)", title_low)
        or "⬆" in title_text
        or re.search(r"\bupdate\b.*\bdependenc", title_low)
    ):
        return "renovate"
    if lowered & {"sync", "template-sync", "consumer-sync", "consumer-hygiene"} or any(
        term in title_low
        for term in ("sync", "template sync", "consumer hygiene", "mirror", "drift")
    ):
        return "sync"
    if (
        lowered & {"ci", "build", "tooling", "workflow", "infra", "automation", "agents"}
        or re.search(r"\b(?:ci|build)\s*(?::|\()", title_low)
        or re.search(r"\bchore\s*\(\s*(?:ci|workflow)\s*\)", title_low)
    ):
        return "tooling"
    if lowered & {"docs", "documentation"} or re.search(r"\bdocs\s*(?::|\()", title_low):
        return "docs"
    return "issue"


def _parse_gh_ts(value: str | None) -> int | None:
    return durability_sweep._parse_gh_ts(value)


def _run_ts(pr: dict) -> int:
    return _parse_gh_ts(pr.get("mergedAt") or pr.get("createdAt")) or int(time.time())


def _stable_run_id(repo: str, pr_number: int, agent: str) -> str:
    return f"keepalive:{repo}#{pr_number}:{agent}"


def _stable_non_agent_run_id(repo: str, pr_number: int) -> str:
    return _stable_run_id(repo, pr_number, NON_AGENT)


def _run_exists(run_id: str) -> bool:
    with feedback._conn() as c:
        row = c.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone()
    return row is not None


def _existing_outcome(run_id: str) -> dict | None:
    with feedback._conn() as c:
        row = c.execute(
            "SELECT merged, adjudicated_verdict, durability, notes FROM outcomes WHERE run_id=?",
            (run_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "merged": row[0],
        "adjudicated_verdict": row[1],
        "durability": row[2],
        "notes": row[3],
    }


def _existing_remote_for_pr(repo: str, pr_number: int) -> str | None:
    target = f"{repo}#{pr_number}"
    target_like = f"{repo}#{pr_number}"
    remote_prefix = f"remote:{repo}#{pr_number}:"
    with feedback._conn() as c:
        row = c.execute(
            "SELECT run_id FROM runs WHERE "
            "(run_id LIKE ? OR target=? OR (pr_number=? AND target=?)) "
            "AND (source='orchestrator_remote' OR mode='remote' OR run_id LIKE 'remote:%') "
            "ORDER BY ts DESC LIMIT 1",
            (remote_prefix + "%", target_like, pr_number, target),
        ).fetchone()
    return row[0] if row else None


def _source_counts() -> dict[str, int]:
    with feedback._conn() as c:
        rows = c.execute(
            "SELECT COALESCE(source,'NULL'), COUNT(*) FROM runs GROUP BY COALESCE(source,'NULL')"
        ).fetchall()
    return {source: count for source, count in rows}


def _record_attribution(summary: IngestSummary, source: str) -> None:
    attribution = summary["attribution"]
    key = "blocked" if source == ATTRIBUTION_UNRESOLVED else "let_through"
    attribution[key] = cast(int, attribution[key]) + 1
    by_source = cast(dict[str, int], attribution["by_source"])
    by_source[source] = int(by_source.get(source, 0)) + 1


def _update_attribution(run_id: str, agent: str, source: str) -> None:
    """Change only attribution fields: historical run IDs stay immutable."""
    with feedback._conn() as c:
        row = c.execute("SELECT routing_metadata FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            return
        metadata = feedback._routing_metadata_dict(row[0])
        metadata["attribution_source"] = source
        c.execute(
            "UPDATE runs SET agent=?, routing_metadata=? WHERE run_id=?",
            (agent, json.dumps(metadata, sort_keys=True), run_id),
        )


def _gh_api_json(path: str) -> object | None:
    """Read-only GitHub API reader, reserved for the explicit historical backfill."""
    return _run_json(["gh", "api", path])


def _backfill_evidence(repo: str, pr_number: int) -> dict:
    """Return evidence readable now; this is the only new live-GitHub access path."""
    pr = _gh_api_json(f"repos/{repo}/pulls/{pr_number}")
    if not isinstance(pr, dict):
        return {}
    labels = _label_names(pr)
    job_names: list[str] = []
    runs = _gh_api_json(
        f"repos/{repo}/actions/workflows/agents-keepalive-loop.yml/runs?event=pull_request&per_page=100"
    )
    if isinstance(runs, dict):
        matching = [
            run
            for run in runs.get("workflow_runs") or []
            if any(str(p.get("number")) == str(pr_number) for p in run.get("pull_requests") or [])
        ]
        if matching:
            newest = max(matching, key=lambda run: int(run.get("id") or 0))
            jobs = _gh_api_json(f"repos/{repo}/actions/runs/{newest['id']}/jobs?per_page=100")
            if isinstance(jobs, dict):
                job_names = [str(job.get("name") or "") for job in jobs.get("jobs") or []]
    return {
        "labels": labels,
        "job_names": job_names,
        "author": _author_login(pr),
        "summary": str(pr.get("body") or ""),
    }


def _backfill_evidence_batch(repo: str, numbers: list[int]) -> dict[int, dict]:
    """One GraphQL query per 40 PRs: branch, author, labels, commit identities, body, createdAt.
    Replaces three REST calls per PR; a failed chunk simply leaves its PRs to the per-PR fallback.
    """
    owner, _, name = repo.partition("/")
    out: dict[int, dict] = {}
    for i in range(0, len(numbers), 40):
        chunk = numbers[i : i + 40]
        fields = " ".join(
            f"p{n}: pullRequest(number:{n}){{ number headRefName createdAt author{{login}} "
            f"labels(first:30){{nodes{{name}}}} commits(last:30){{nodes{{commit{{author{{name email "
            f"user{{login}}}} message}}}}}} body }}"
            for n in chunk
        )
        query = f'query {{ repository(owner:"{owner}", name:"{name}") {{ {fields} }} }}'
        data = _run_json(["gh", "api", "graphql", "-f", f"query={query}"], timeout=120)
        payload = data.get("data") if isinstance(data, dict) else None
        repo_data = payload.get("repository") if isinstance(payload, dict) else None
        for pr in (repo_data or {}).values():
            if not isinstance(pr, dict) or pr.get("number") is None:
                continue
            labels_node = pr.get("labels") if isinstance(pr.get("labels"), dict) else {}
            labels = [
                str(n.get("name"))
                for n in labels_node.get("nodes") or []
                if isinstance(n, dict) and n.get("name")
            ]
            author_node = pr.get("author") if isinstance(pr.get("author"), dict) else {}
            out[int(pr["number"])] = {
                "labels": labels,
                "job_names": [],
                "author": author_node.get("login"),
                "summary": str(pr.get("body") or ""),
                "head_ref": str(pr.get("headRefName") or ""),
                "commit_identities": _commit_identities(pr),
                "created_ts": _created_epoch(pr),
            }
    return out


BACKFILL_LIMIT_DEFAULT = 200


def backfill_attribution(
    *,
    apply: bool = False,
    backfill_limit: int = BACKFILL_LIMIT_DEFAULT,
    _evidence_fetch_fn=None,
) -> dict:
    """Re-derive existing keepalive:none rows without changing their stable run IDs."""
    fetch_evidence = _evidence_fetch_fn or _backfill_evidence
    with feedback._conn() as c:
        rows = c.execute(
            "SELECT run_id, target, routing_metadata FROM runs WHERE source='keepalive' AND agent=? "
            "ORDER BY run_id DESC",
            (NON_AGENT,),
        ).fetchall()
    pending: list[tuple[str, str]] = []
    for run_id, target, routing_metadata_raw in rows:
        metadata = feedback._routing_metadata_dict(routing_metadata_raw)
        existing_source = metadata.get("attribution_source")
        if existing_source and existing_source != ATTRIBUTION_UNRESOLVED:
            continue
        pending.append((str(run_id), str(target or "")))
    before = len(pending)
    summary: dict[str, Any] = {
        "before_none": before,
        "after_none": before,
        "let_through": 0,
        "blocked": 0,
        "by_source": {},
        "examined": 0,
        "resolved": 0,
        "unresolved": 0,
        "remaining": before,
        "backfill_limit": backfill_limit,
    }
    # Prefetch in batches: the rows this call will examine, grouped by repository.
    batch: dict[tuple[str, int], dict] = {}
    if _evidence_fetch_fn is None:
        by_repo: dict[str, list[int]] = {}
        for _run_id, target in pending[:backfill_limit]:
            match = re.fullmatch(r"([^#]+)#(\d+)", target)
            if match:
                by_repo.setdefault(match.group(1), []).append(int(match.group(2)))
        for repo, numbers in by_repo.items():
            for number, evidence in _backfill_evidence_batch(repo, numbers).items():
                batch[(repo, number)] = evidence
    examined = 0
    for run_id, target in pending:
        if examined >= backfill_limit:
            break
        examined += 1
        if examined % 25 == 0:
            print(
                f"backfill_attribution: examined {examined}/{min(backfill_limit, before)} "
                f"(resolved={summary['resolved']} unresolved={summary['unresolved']})",
                file=sys.stderr,
            )
        match = re.fullmatch(r"([^#]+)#(\d+)", target)
        if not match:
            summary["blocked"] += 1
            summary["unresolved"] += 1
            summary["by_source"][ATTRIBUTION_UNRESOLVED] = (
                summary["by_source"].get(ATTRIBUTION_UNRESOLVED, 0) + 1
            )
            continue
        repo, number = match.group(1), int(match.group(2))
        evidence = batch.get((repo, number)) or fetch_evidence(repo, number) or {}
        agent, source = derive_attribution(
            list(evidence.get("labels") or []),
            job_names=list(evidence.get("job_names") or []),
            author=evidence.get("author"),
            summary=evidence.get("summary"),
            head_ref=evidence.get("head_ref"),
            commit_identities=list(evidence.get("commit_identities") or []),
            repo=repo,
            created_ts=evidence.get("created_ts"),
        )
        summary["by_source"][source] = summary["by_source"].get(source, 0) + 1
        if source == ATTRIBUTION_UNRESOLVED:
            summary["blocked"] += 1
            summary["unresolved"] += 1
            continue
        summary["let_through"] += 1
        summary["resolved"] += 1
        if apply:
            _update_attribution(run_id, agent, source)
    summary["examined"] = examined
    summary["remaining"] = max(0, before - examined)
    summary["after_none"] = before - summary["let_through"] if apply else before
    summary["applied"] = apply
    print(
        "backfill_attribution: "
        f"examined={examined} resolved={summary['resolved']} "
        f"unresolved={summary['unresolved']} remaining={summary['remaining']}",
        file=sys.stderr,
    )
    return summary


def _gh_throttle(resource: str) -> None:
    """Pace/defer against the shared GitHub rate budget (gh_capacity) when ORCH_GH_THROTTLE=1;
    no-op + fail-open otherwise so the ingest never breaks on a missing/erroring module."""
    try:
        import gh_capacity

        gh_capacity.throttle_if_enabled(resource)
    except Exception:
        pass


def _outcome_for_pr(
    repo: str,
    pr: dict,
    run: dict,
    *,
    now: int | None = None,
    _revert_fn=None,
    revert_cache: dict | None = None,
) -> dict | None:
    oc = outcomes.state_to_outcome(pr)
    if not oc:
        return None
    if oc.get("merged"):
        pr_for_durability = dict(pr)
        pr_for_durability.setdefault("repo", repo)
        pr_for_durability.setdefault("number", pr.get("number"))
        verdict = durability_sweep.classify_durability(
            run, pr_for_durability, now=now, _revert_fn=_revert_fn, revert_cache=revert_cache
        )
        if verdict.get("durability"):
            oc["durability"] = verdict["durability"]
            oc["notes"] = verdict.get("notes")
    return oc


def _should_record_outcome(existing: dict | None, oc: dict | None) -> bool:
    if oc is None:
        return False
    if existing is None:
        return True
    existing_durability = existing.get("durability")
    new_durability = oc.get("durability")
    if existing_durability in (None, "pending") and new_durability not in (None, "pending"):
        return True
    if process_suppression_reason(oc.get("notes")) and not process_suppression_reason(
        existing.get("notes")
    ):
        return True
    return False


def ingest_keepalive_outcomes(
    repos: list[str] | None = None,
    *,
    lookback_days: int = 30,
    dry_run: bool = False,
    _pr_fetch_fn=None,
    _now: int | None = None,
    _revert_fn=None,
    include_non_agent: bool = False,
    _closure_context_fn=None,
) -> IngestSummary:
    repos = repos or _active_repos()
    pr_fetch_fn = _pr_fetch_fn or _fetch_prs
    closure_context_fn = _closure_context_fn or _fetch_pr_context
    revert_fn = _revert_fn
    if dry_run and revert_fn is None:

        def revert_fn(_pr):
            return (None, "dry-run skips live revert scan")

    revert_cache: dict = {}  # repo -> cached revert search; 1 search/repo across the ingest
    summary: IngestSummary = {
        "repos": len(repos),
        "prs_seen": 0,
        "runs_recorded": 0,
        "outcomes_recorded": 0,
        "skipped_existing": 0,
        "non_agent_prs_seen": 0,
        "non_agent_runs_recorded": 0,
        "by_source": {},
        "attribution": {"let_through": 0, "blocked": 0, "by_source": {}},
    }

    for repo in repos:
        _gh_throttle("core")  # `gh pr list` per repo = CORE (5000/hr)
        prs = pr_fetch_fn(repo, lookback_days)
        if not isinstance(prs, list):
            prs = []
        for pr in prs:
            labels = _label_names(pr)
            agent, attribution_source = derive_attribution(
                labels,
                author=_author_login(pr),
                summary=str(pr.get("body") or ""),
                head_ref=str(pr.get("headRefName") or ""),
                commit_identities=_commit_identities(pr),
                repo=repo,
                created_ts=_created_epoch(pr),
            )
            pr_number = pr.get("number")
            if pr_number is None:
                continue
            pr_number = int(pr_number)
            if agent == NON_AGENT:
                if not include_non_agent:
                    continue
                target = f"{repo}#{pr_number}"
                run_id = _stable_non_agent_run_id(repo, pr_number)
                run = {
                    "run_id": run_id,
                    "target": target,
                    "mode": "remote",
                    "pr_number": pr_number,
                }
                oc = _outcome_for_pr(
                    repo, pr, run, now=_now, _revert_fn=revert_fn, revert_cache=revert_cache
                )
                # Non-agent rows are process evidence, not active assignments; skip open PRs until
                # they become terminal so the Brain does not accumulate unlabeled in-flight noise.
                if oc is None:
                    continue
                summary["prs_seen"] += 1
                summary["non_agent_prs_seen"] += 1
                _record_attribution(summary, attribution_source)
                if _existing_remote_for_pr(repo, pr_number):
                    summary["skipped_existing"] += 1
                    continue

                task_type = _task_type_from_labels(labels)
                work_type = _work_type(labels, str(pr.get("title") or ""), _author_login(pr))
                oc = _maybe_mark_process_ignore(repo, pr, work_type, oc, closure_context_fn)
                run_already_exists = _run_exists(run_id)
                existing_oc = _existing_outcome(run_id)

                if run_already_exists:
                    summary["skipped_existing"] += 1
                elif not dry_run:
                    feedback.record_run(
                        run_id,
                        target,
                        task_type,
                        NON_AGENT,
                        mode="remote",
                        rationale="keepalive-discovered non-agent PR",
                        pr_number=pr_number,
                        ts=_run_ts(pr),
                        model=None,
                        source="keepalive",
                        assignment=NON_AGENT,
                        work_type=work_type,
                        routing_metadata={"attribution_source": attribution_source},
                    )
                    summary["runs_recorded"] += 1
                    summary["non_agent_runs_recorded"] += 1
                elif dry_run:
                    summary["runs_recorded"] += 1
                    summary["non_agent_runs_recorded"] += 1

                if oc is not None and _should_record_outcome(existing_oc, oc):
                    if not dry_run:
                        feedback.record_outcome(run_id, **oc)
                    summary["outcomes_recorded"] += 1
                continue

            summary["prs_seen"] += 1
            _record_attribution(summary, attribution_source)
            if _existing_remote_for_pr(repo, pr_number):
                summary["skipped_existing"] += 1
                continue

            task_type = _task_type_from_labels(labels)
            work_type = _work_type(labels, str(pr.get("title") or ""), _author_login(pr))
            target = f"{repo}#{pr_number}"
            run_id = _stable_run_id(repo, pr_number, agent)
            run = {"run_id": run_id, "target": target, "mode": "remote", "pr_number": pr_number}
            run_already_exists = _run_exists(run_id)
            oc = _outcome_for_pr(
                repo, pr, run, now=_now, _revert_fn=revert_fn, revert_cache=revert_cache
            )
            oc = _maybe_mark_process_ignore(repo, pr, work_type, oc, closure_context_fn)
            existing_oc = _existing_outcome(run_id)

            if run_already_exists:
                summary["skipped_existing"] += 1
            elif not dry_run:
                feedback.record_run(
                    run_id,
                    target,
                    task_type,
                    agent,
                    mode="remote",
                    rationale="keepalive-discovered agent PR",
                    pr_number=pr_number,
                    ts=_run_ts(pr),
                    model=None,
                    source="keepalive",
                    assignment="assigned",
                    work_type=work_type,
                    routing_metadata={"attribution_source": attribution_source},
                )
                summary["runs_recorded"] += 1
            elif dry_run:
                summary["runs_recorded"] += 1

            if oc is not None and _should_record_outcome(existing_oc, oc):
                if not dry_run:
                    feedback.record_outcome(run_id, **oc)
                summary["outcomes_recorded"] += 1

    summary["by_source"] = _source_counts()
    return summary


def _iso_days_ago(now: int, days: int) -> str:
    dt = _dt.datetime.fromtimestamp(now - days * 86400, _dt.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _selftest() -> None:
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp(prefix="keepalive-outcomes-selftest-")
    old_db = feedback.DB_PATH
    feedback.DB_PATH = Path(tmp) / "t.db"
    now = int(time.time())
    try:
        old = _iso_days_ago(now, 10)
        young = _iso_days_ago(now, 2)
        assert _work_type([], "chore(deps): update requests", "renovate[bot]") == "renovate"
        assert _work_type(["consumer-sync"], "Mirror template drift", None) == "sync"
        assert _work_type(["infra"], "ci: tighten workflow", None) == "tooling"
        assert _work_type(["documentation"], "docs: refresh guide", None) == "docs"
        assert _work_type(["agent:codex"], "Fix export edge case", "teacher") == "issue"
        label = derive_attribution(["agent:codex"])
        assert label == ("codex", "agent_label"), label
        assert derive_attribution(["agents:tried-claude"]) == ("claude", "tried_label")
        assert derive_attribution([], job_names=["run-cursor"]) == ("cursor", "keepalive_job")
        assert derive_attribution(["agent:codex", "agent:claude"]) == ("none", "unresolved")
        assert derive_attribution([], author="chatgpt-codex-connector") == (
            "codex",
            "author_login",
        )
        assert derive_attribution([], summary="## Automated Status Summary\n**Agent:** Gemini") == (
            "gemini",
            "automated_status_summary",
        )
        assert derive_attribution([], author="stranske-keepalive[bot]") == (
            "none",
            "unresolved",
        )
        # The evidence classes added 2026-09-15, in priority order.
        assert derive_attribution(["agent:claude"], head_ref="codex/x") == ("claude", "agent_label")
        assert derive_attribution([], head_ref="codex/issue-554-guard") == (
            "codex",
            "branch_prefix",
        )
        assert derive_attribution([], head_ref="sync/workflows-delivery") == (
            "none",
            "bot:template-sync",
        )
        assert derive_attribution([], head_ref="fix/x", author="renovate[bot]") == (
            "none",
            "bot:renovate",
        )
        assert derive_attribution(
            [],
            head_ref="fix/x",
            commit_identities=["cursoragent", "co-authored-by: cursor <c@cursor.com>"],
        ) == ("cursor", "commit_identity")
        assert derive_attribution([], commit_identities=["claude", "cursoragent"]) == (
            "none",
            "unresolved",
        )  # two agents' identities on one PR is ambiguity, not a vote
        assert derive_attribution(
            [], head_ref="fix/x", commit_identities=["tim stranske", "stranske", "tim@example.com"]
        ) == ("none", "human")
        assert derive_attribution([], commit_identities=["tim stranske", "closer-lane"]) == (
            "none",
            "unresolved",
        )  # a lane commit beside the owner's is not the owner's own PR
        feedback.record_run(
            "local-42",
            "o/r#42",
            "implement",
            "vibe",
            mode="full",
            ts=now - 600,
            source="orchestrator_local",
        )
        assert derive_attribution(
            [], head_ref="orchestrator/issue-42", repo="o/r", created_ts=now
        ) == (
            "vibe",
            "local_dispatch_join",
        )
        assert derive_attribution(
            [], head_ref="orchestrator/issue-43", repo="o/r", created_ts=now
        ) == (
            "none",
            "unresolved",
        )
        assert _commit_identities(
            {
                "commits": [
                    {
                        "authors": [{"login": "", "name": "closer-lane", "email": "a@b"}],
                        "messageBody": "x\n\nCo-Authored-By: Claude Opus 5 <n@a>",
                    }
                ]
            }
        ) == ["closer-lane", "a@b", "co-authored-by: claude opus 5 <n@a>"]
        old_notes = {"durability": "abandoned", "notes": "remote keepalive PR closed unmerged"}
        tagged_notes = {
            "durability": "abandoned",
            "notes": "remote keepalive PR closed unmerged; process_ignore=duplicate_or_superseded",
        }
        assert _should_record_outcome(old_notes, tagged_notes)
        assert not _should_record_outcome(tagged_notes, tagged_notes)
        prs = {
            "o/r": [
                {
                    "number": 1,
                    "state": "MERGED",
                    "labels": [{"name": "agent:codex"}],
                    "title": "Fix durable issue",
                    "createdAt": old,
                    "mergedAt": old,
                    "baseRefName": "main",
                    "mergeCommit": {"oid": "aaa"},
                    "reverted": False,
                },
                {
                    "number": 2,
                    "state": "MERGED",
                    "labels": [{"name": "agent:claude"}],
                    "title": "chore(deps): update requests",
                    "author": {"login": "renovate[bot]"},
                    "createdAt": young,
                    "mergedAt": young,
                    "baseRefName": "main",
                    "mergeCommit": {"oid": "bbb"},
                    "reverted": False,
                },
                {
                    "number": 3,
                    "state": "CLOSED",
                    "labels": [{"name": "agent:cursor"}],
                    "title": "Fix abandoned issue",
                    "createdAt": old,
                    "closedAt": old,
                },
                {
                    "number": 4,
                    "state": "MERGED",
                    "labels": [{"name": "agent:vibe"}],
                    "title": "Fix skipped remote",
                    "createdAt": old,
                    "mergedAt": old,
                    "baseRefName": "main",
                    "mergeCommit": {"oid": "ccc"},
                    "reverted": False,
                },
                {
                    "number": 5,
                    "state": "MERGED",
                    "labels": [],
                    "title": "chore(deps): update requests",
                    "author": {"login": "renovate[bot]"},
                    "createdAt": old,
                    "mergedAt": old,
                    "baseRefName": "main",
                    "mergeCommit": {"oid": "ddd"},
                    "reverted": False,
                },
                {
                    "number": 6,
                    "state": "CLOSED",
                    "labels": [{"name": "template-sync"}],
                    "title": "Sync templates",
                    "author": {"login": "github-actions[bot]"},
                    "createdAt": old,
                    "closedAt": old,
                },
                {
                    "number": 7,
                    "state": "OPEN",
                    "labels": [],
                    "title": "Open human PR",
                    "author": {"login": "teacher"},
                    "createdAt": young,
                },
            ]
        }

        feedback.record_run(
            "remote:o/r#4:vibe",
            "o/r#4",
            "implement",
            "vibe",
            mode="remote",
            pr_number=4,
            source="orchestrator_remote",
        )

        res = ingest_keepalive_outcomes(
            ["o/r"],
            _pr_fetch_fn=lambda repo, days: prs.get(repo, []),
            _now=now,
        )
        assert res["prs_seen"] == 4, res
        assert res["runs_recorded"] == 3, res
        assert res["outcomes_recorded"] == 3, res
        assert res["skipped_existing"] == 1, res
        assert res["attribution"]["let_through"] == 4, res
        with feedback._conn() as c:
            rows = {
                rid: (source, assignment, work_type, durability)
                for rid, source, assignment, work_type, durability in c.execute(
                    "SELECT r.run_id, r.source, r.assignment, r.work_type, o.durability "
                    "FROM runs r LEFT JOIN outcomes o ON r.run_id=o.run_id"
                ).fetchall()
            }
            source_cols = [
                row[1]
                for row in c.execute("PRAGMA table_info(runs)").fetchall()
                if row[1] == "source"
            ]
            assignment_cols = [
                row[1]
                for row in c.execute("PRAGMA table_info(runs)").fetchall()
                if row[1] == "assignment"
            ]
            work_type_cols = [
                row[1]
                for row in c.execute("PRAGMA table_info(runs)").fetchall()
                if row[1] == "work_type"
            ]
        assert rows["keepalive:o/r#1:codex"] == ("keepalive", "assigned", "issue", "durable"), rows
        assert rows["keepalive:o/r#2:claude"] == (
            "keepalive",
            "assigned",
            "renovate",
            "pending",
        ), rows
        assert rows["keepalive:o/r#3:cursor"] == (
            "keepalive",
            "assigned",
            "issue",
            "abandoned",
        ), rows
        assert "keepalive:o/r#4:vibe" not in rows, rows
        assert rows["remote:o/r#4:vibe"][:2] == ("orchestrator_remote", "experimental"), rows
        assert len(source_cols) == 1, source_cols
        assert len(assignment_cols) == 1, assignment_cols
        assert len(work_type_cols) == 1, work_type_cols

        res2 = ingest_keepalive_outcomes(
            ["o/r"],
            _pr_fetch_fn=lambda repo, days: prs.get(repo, []),
            _now=now,
        )
        assert res2["runs_recorded"] == 0 and res2["outcomes_recorded"] == 0, res2

        with feedback._conn() as c:
            before = [row[1] for row in c.execute("PRAGMA table_info(runs)").fetchall()].count(
                "source"
            )
            before_assignment = [
                row[1] for row in c.execute("PRAGMA table_info(runs)").fetchall()
            ].count("assignment")
            before_work_type = [
                row[1] for row in c.execute("PRAGMA table_info(runs)").fetchall()
            ].count("work_type")
            feedback._migrate_schema(c)
            after = [row[1] for row in c.execute("PRAGMA table_info(runs)").fetchall()].count(
                "source"
            )
            after_assignment = [
                row[1] for row in c.execute("PRAGMA table_info(runs)").fetchall()
            ].count("assignment")
            after_work_type = [
                row[1] for row in c.execute("PRAGMA table_info(runs)").fetchall()
            ].count("work_type")
        assert before == 1 and after == 1, (before, after)
        assert before_assignment == 1 and after_assignment == 1, (
            before_assignment,
            after_assignment,
        )
        assert before_work_type == 1 and after_work_type == 1, (before_work_type, after_work_type)

        res3 = ingest_keepalive_outcomes(
            ["o/r"],
            _pr_fetch_fn=lambda repo, days: prs.get(repo, []),
            _now=now,
            include_non_agent=True,
            _closure_context_fn=lambda _repo, pr_number: (
                "Closing as duplicate: PR #5 is the canonical opener." if pr_number == 6 else ""
            ),
        )
        assert res3["non_agent_prs_seen"] == 2, res3
        assert res3["non_agent_runs_recorded"] == 2, res3
        assert res3["runs_recorded"] == 2, res3
        assert res3["outcomes_recorded"] == 2, res3
        with feedback._conn() as c:
            non_agent_rows = {
                rid: (agent, source, assignment, work_type, durability, notes)
                for rid, agent, source, assignment, work_type, durability, notes in c.execute(
                    "SELECT r.run_id, r.agent, r.source, r.assignment, r.work_type, "
                    "o.durability, COALESCE(o.notes,'') "
                    "FROM runs r LEFT JOIN outcomes o ON r.run_id=o.run_id "
                    "WHERE r.assignment='none'"
                ).fetchall()
            }
        assert non_agent_rows["keepalive:o/r#5:none"][:5] == (
            "none",
            "keepalive",
            "none",
            "renovate",
            "durable",
        ), non_agent_rows
        assert (
            PROCESS_IGNORE_MARKER not in non_agent_rows["keepalive:o/r#5:none"][5]
        ), non_agent_rows
        assert non_agent_rows["keepalive:o/r#6:none"][:5] == (
            "none",
            "keepalive",
            "none",
            "sync",
            "abandoned",
        ), non_agent_rows
        assert PROCESS_IGNORE_MARKER in non_agent_rows["keepalive:o/r#6:none"][5], non_agent_rows
        assert "keepalive:o/r#7:none" not in non_agent_rows, non_agent_rows

        feedback.record_run(
            "keepalive:o/r#8:none", "o/r#8", "implement", "none", mode="remote", source="keepalive"
        )
        feedback.record_run(
            "keepalive:o/r#9:none", "o/r#9", "implement", "none", mode="remote", source="keepalive"
        )
        evidence = {
            ("o/r", 8): {"labels": [], "job_names": ["run-claude"]},
            ("o/r", 9): {"labels": [], "author": "stranske-keepalive[bot]"},
        }
        preview = backfill_attribution(
            _evidence_fetch_fn=lambda repo, num: evidence.get((repo, num), {})
        )
        # PR #5 (author renovate[bot]) is classified bot:renovate AT INGEST since 2026-09-15, so it is
        # no longer pending: 3 unresolved rows remain (#6 template-sync by github-actions, #8, #9).
        with feedback._conn() as c:
            renovate_row = c.execute(
                "SELECT agent, routing_metadata FROM runs WHERE run_id='keepalive:o/r#5:none'"
            ).fetchone()
        assert renovate_row[0] == "none", renovate_row
        assert json.loads(renovate_row[1])["attribution_source"] == "bot:renovate", renovate_row
        assert preview["before_none"] == 3 and preview["after_none"] == 3, preview
        assert preview["let_through"] == 1 and preview["blocked"] == 2, preview
        applied = backfill_attribution(
            apply=True, _evidence_fetch_fn=lambda repo, num: evidence.get((repo, num), {})
        )
        assert applied["after_none"] == 2 and applied["let_through"] == 1, applied
        with feedback._conn() as c:
            restored = c.execute(
                "SELECT agent, routing_metadata FROM runs WHERE run_id='keepalive:o/r#8:none'"
            ).fetchone()
        assert restored[0] == "claude", restored
        assert json.loads(restored[1])["attribution_source"] == "keepalive_job", restored

        fetch_calls: list[tuple[str, int]] = []

        def counting_fetch(repo, num):
            fetch_calls.append((repo, num))
            return {}

        for idx in range(5):
            feedback.record_run(
                f"keepalive:o/r#{idx}:none",
                f"o/r#{idx}",
                "implement",
                "none",
                mode="remote",
                source="keepalive",
            )
        limited = backfill_attribution(
            backfill_limit=2,
            _evidence_fetch_fn=counting_fetch,
        )
        assert limited["examined"] == 2, limited
        assert limited["remaining"] == limited["before_none"] - limited["examined"], limited
        assert len(fetch_calls) == 2, fetch_calls
        assert fetch_calls[0][0] == "o/r", fetch_calls

        print("keepalive_outcomes.py selftest: OK")
    finally:
        feedback.DB_PATH = old_db
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Ingest keepalive PR outcomes into feedback.db.")
    parser.add_argument(
        "--repos", nargs="+", help="owner/repo values; defaults to active registry repos"
    )
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--include-non-agent",
        action="store_true",
        help="also ingest terminal unlabeled/bot/human PRs as source=keepalive assignment=none",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument(
        "--backfill-attribution",
        action="store_true",
        help="re-derive source=keepalive agent=none rows; dry-run unless --apply is present",
    )
    parser.add_argument(
        "--backfill-limit",
        type=int,
        default=BACKFILL_LIMIT_DEFAULT,
        help="max unresolved rows to examine per invocation (newest first; default 200)",
    )
    parser.add_argument(
        "--apply", action="store_true", help="allow --backfill-attribution to write"
    )
    args = parser.parse_args(argv)

    if args.selftest:
        _selftest()
        return 0

    if args.backfill_attribution:
        result = backfill_attribution(apply=args.apply, backfill_limit=args.backfill_limit)
        print(json.dumps(result, indent=2, sort_keys=True) if args.json else result)
        return 0

    res = ingest_keepalive_outcomes(
        args.repos,
        lookback_days=args.lookback_days,
        dry_run=args.dry_run,
        include_non_agent=args.include_non_agent,
    )
    if args.json:
        print(json.dumps(res, indent=2, sort_keys=True))
    else:
        print(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
