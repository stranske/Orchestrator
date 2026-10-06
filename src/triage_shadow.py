#!/usr/bin/env python3
"""Compare the existing TriageAgent with opener ordering; never drive a worker.

The active tick calls this independently of ORCH_DISPATCH_LANE. GitHub access
is search/read only. Observations are local, and neither ranking changes the
opener's selection. ORCH_TRIAGE_SHADOW=0 disables the caller.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import backlog
import feedback
import outcomes
import roles


def corpus_path() -> Path:
    return Path(os.environ.get("ORCH_STATE_DIR", str(Path.home() / ".codex/orchestrator"))) / (
        "capability-program/triage-shadow.jsonl"
    )


def rule_key(item: dict) -> tuple:
    labels = set(backlog._labels(item))
    tier = next(
        (i for i, name in enumerate(("high", "normal", "low")) if "priority:" + name in labels), 3
    )
    return tier, item.get("createdAt", ""), "repo-review-approved" not in labels, item["target"]


def candidates_from_search(
    rows: list[dict],
    supported: list[str],
    *,
    scoped: set[str] | None = None,
    linked: set[str] | None = None,
) -> list[dict]:
    candidates = {}
    for row in rows:
        repo = row.get("repository") or ""
        if isinstance(repo, dict):
            repo = repo.get("nameWithOwner", "")
        labels = backlog._labels(row)
        title = row.get("title", "")
        if repo not in supported or set(labels) & {"needs-human", "agents:paused", "deeper_review"}:
            continue
        if title.startswith("⚠️ CODEX_AUTH_JSON expires"):
            continue
        target = f"{repo}#{row['number']}"
        if repo in (scoped or set()) or target in (scoped or set()) or target in (linked or set()):
            continue
        candidates[target] = {
            **row,
            "repository": repo,
            "target": target,
            "lane": "opener",
            "task_type": backlog.classify(labels),
        }
    return sorted(candidates.values(), key=rule_key)


def discover_candidates(run=None) -> list[dict]:
    """Use the opener's three searches; refuse a truncated or failed population."""
    run = run or subprocess.run
    rows = []
    for tier in ("high", "normal", "low"):
        proc = run(
            [
                "gh",
                "search",
                "issues",
                "--owner",
                "stranske",
                "--label",
                "priority:" + tier,
                "--state",
                "open",
                "--json",
                "repository,number,title,labels,createdAt,body",
                "--limit",
                "1000",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode:
            raise RuntimeError(proc.stderr.strip() or "issue search failed")
        found = json.loads(proc.stdout)
        if not isinstance(found, list) or len(found) >= 1000:
            raise ValueError("issue search population is malformed or truncated")
        for item in found:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("repository"), dict)
                or not isinstance(item["repository"].get("nameWithOwner"), str)
                or not item["repository"]["nameWithOwner"]
            ):
                raise ValueError("issue search repository is UNKNOWN")
        rows.extend(found)
    # Map closing references, body references and branch source identifiers using
    # the existing intake resolver. A read failure never means "unlinked".
    linked = set()
    for repo in sorted(
        {
            row["repository"]["nameWithOwner"]
            for row in rows
            if row["repository"]["nameWithOwner"] in backlog.SUPPORTED_REPOS
        }
    ):
        proc = run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--limit",
                "1000",
                "--json",
                "number,body,headRefName,closingIssuesReferences",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode:
            raise RuntimeError(proc.stderr.strip() or "PR linkage read failed")
        prs = json.loads(proc.stdout)
        if not isinstance(prs, list) or len(prs) >= 1000:
            raise ValueError("PR linkage population is malformed or truncated")
        for pr in prs:
            linked.update(backlog._issue_targets(repo, pr))
    if backlog.scoped_blocker_source() not in ("ok", "absent"):
        raise ValueError("scoped blocker population is UNKNOWN")
    scoped = backlog.load_scoped_blockers() | {
        target
        for target, entry in backlog.scoped_blocker_entries().items()
        if isinstance(entry, dict) and entry.get("await_human")
    }
    candidates = candidates_from_search(rows, backlog.SUPPORTED_REPOS, scoped=scoped, linked=linked)
    # A still-open source already delivered through a merged closing PR is
    # verifier work, not another implementation candidate.
    eligible = []
    for item in candidates:
        owner, repo = item["repository"].split("/", 1)
        query = (
            "query($owner:String!,$repo:String!,$n:Int!){repository(owner:$owner,name:$repo){"
            "issue(number:$n){closedByPullRequestsReferences(first:100){nodes{state} "
            "pageInfo{hasNextPage}}}}}"
        )
        proc = run(
            [
                "gh",
                "api",
                "graphql",
                "-f",
                "query=" + query,
                "-f",
                "owner=" + owner,
                "-f",
                "repo=" + repo,
                "-F",
                "n=" + str(item["number"]),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode:
            raise RuntimeError(proc.stderr.strip() or "merged linkage read failed")
        data = json.loads(proc.stdout)
        if not isinstance(data, dict) or data.get("errors"):
            raise ValueError("merged linkage population is UNKNOWN")
        try:
            refs = data["data"]["repository"]["issue"]["closedByPullRequestsReferences"]
            if refs["pageInfo"]["hasNextPage"] is True:
                raise ValueError("merged linkage population is truncated")
            if refs["pageInfo"]["hasNextPage"] is not False:
                raise ValueError("merged linkage pagination is UNKNOWN")
            nodes = refs["nodes"]
            if not isinstance(nodes, list) or any(
                not isinstance(pr, dict) or pr.get("state") not in {"OPEN", "CLOSED", "MERGED"}
                for pr in nodes
            ):
                raise ValueError("merged linkage states are UNKNOWN")
            if not any(pr["state"] == "MERGED" for pr in nodes):
                eligible.append(item)
        except (KeyError, TypeError) as exc:
            raise ValueError("merged linkage population is UNKNOWN") from exc
    return eligible


def record_cycle(
    items: list[dict],
    *,
    path: Path | None = None,
    runner=None,
    dispatch: bool = True,
    proposal_json: dict | None = None,
) -> dict:
    """One complete snapshot, including invalid proposals as named UNKNOWNs."""
    ordered = sorted(items, key=rule_key)
    path = path or corpus_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    runner = runner or roles.run_triage_agent
    result: dict[str, Any] = {}
    errors = []
    if ordered:
        # The role's offload gets a disposable private cwd, never the control plane.
        with tempfile.TemporaryDirectory(prefix="triage-shadow-") as private_cwd:
            try:
                result = runner(
                    backlog_items=ordered,
                    max_items=len(ordered),
                    context="Rank this opener snapshot in shadow only; no worker action.",
                    cwd=private_cwd,
                    dispatch=dispatch,
                    proposal_json=proposal_json,
                )
            except Exception as exc:
                errors = [str(exc)]
    errors.extend(result.get("errors") or [])
    proposal = result.get("proposal")
    valid = bool(proposal is not None and not errors)
    recs = (proposal or {}).get("recommendations") or []
    known = {item["target"] for item in ordered}
    rec_targets = [rec.get("target") for rec in recs]
    if valid and (set(rec_targets) != known or len(rec_targets) != len(known)):
        valid = False
        errors.append("triage proposal did not cover the exact candidate population")
    rank = []
    if valid:
        work_now = [rec for rec in recs if rec.get("action") == "work_now"]
        if any(
            not isinstance(rec.get("priority"), int)
            or not 1 <= rec["priority"] <= 5
            for rec in work_now
        ):
            valid = False
            errors.append("triage proposal priority must be an integer from 1 to 5")
        else:
            rank = sorted(
                work_now,
                key=lambda rec: (rec["priority"], rec["target"]),
            )
    row = {
        "schema_version": 1,
        "ts": time.time_ns(),
        "shadow": True,
        "candidate_count": len(ordered),
        "candidate_targets": [i["target"] for i in ordered],
        "rule_pick": ordered[0]["target"] if ordered else None,
        "triage_top_three": [rec["target"] for rec in rank[:3]],
        "triage_valid": valid,
        "errors": errors,
        "live_proposal": bool(
            dispatch and valid and result.get("backend_run_id") and result.get("role_run_id")
        ),
        "role_run_id": result.get("role_run_id"),
        "backend_run_id": result.get("backend_run_id"),
        "backend": result.get("backend"),
        "decision_source": result.get("decision_source"),
        "snapshot_sha256": hashlib.sha256(json.dumps(ordered, sort_keys=True).encode()).hexdigest(),
        "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "recommendations": recs if valid else [],
    }
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, sort_keys=True) + "\n")
    return row


def summary(path: Path | None = None) -> dict:
    """Score only judged target outcomes; a merge with pending durability is unknown."""
    path = path or corpus_path()
    rows = []
    malformed = 0
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                if (
                    not isinstance(row, dict)
                    or row.get("schema_version") != 1
                    or not isinstance(row.get("triage_top_three"), list)
                ):
                    raise ValueError("unknown or malformed row schema")
                rows.append(row)
            except (ValueError, TypeError):
                malformed += 1
    totals = {arm: {"judged": 0, "merged_durable": 0} for arm in ("triage", "rule")}
    live_rows = [row for row in rows if row.get("triage_valid") and row.get("live_proposal")]
    if not live_rows:
        return {
            "cycles": len(rows),
            "malformed_rows": malformed,
            "valid_cycles": sum(bool(row.get("triage_valid")) for row in rows),
            **totals,
        }
    with feedback._conn() as conn:
        for row in live_rows:
            # Invalid/fallback output is never a triage observation, nor a paired comparison.
            if not row.get("triage_valid") or not row.get("live_proposal"):
                continue
            picks = {
                "rule": row.get("rule_pick"),
                "triage": next(iter(row.get("triage_top_three") or []), None),
            }
            for arm, target in picks.items():
                if not target:
                    continue
                found = conn.execute(
                    "SELECT o.merged,o.durability,o.failure_class FROM runs r "
                    "JOIN outcomes o ON o.run_id=r.run_id WHERE r.target=? "
                    "AND r.mode != 'role' ORDER BY r.ts DESC LIMIT 1",
                    (target,),
                ).fetchone()
                if (
                    found
                    and found[1] not in (None, "pending")
                    and (found[2] not in feedback.LEARNING_EXCLUDED_FAILURE_CLASSES)
                ):
                    totals[arm]["judged"] += 1
                    totals[arm]["merged_durable"] += int(found[0] == 1 and found[1] == "durable")
    return {
        "cycles": len(rows),
        "malformed_rows": malformed,
        "valid_cycles": sum(bool(row.get("triage_valid")) for row in rows),
        **totals,
    }


def summary_line(path: Path | None = None) -> str:
    try:
        data = summary(path)
    except (OSError, sqlite3.Error) as exc:
        return f"triage shadow: UNKNOWN — outcome evidence unreadable ({type(exc).__name__})"

    def percent(arm):
        count = data[arm]["judged"]
        return (
            f"{100 * data[arm]['merged_durable'] / count:.1f}% ({count} judged)"
            if count
            else "n/a (0 judged)"
        )

    return (
        f"triage shadow: cycles {data['cycles']}, valid {data['valid_cycles']}, "
        f"triage top-1 merged+durable {percent('triage')}, rule pick {percent('rule')}, "
        f"malformed rows {data['malformed_rows']}"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--proposal-json", type=Path)
    parser.add_argument("--backfill", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        with tempfile.TemporaryDirectory() as tmp:
            row = record_cycle([], path=Path(tmp) / "shadow.jsonl", dispatch=False)
            assert row["candidate_count"] == 0 and row["rule_pick"] is None
        print("triage_shadow.py selftest: OK (empty cycle recorded, no agent or worker mutation)")
        return 0
    if args.summary:
        print(summary_line())
        return 0
    if os.environ.get("ORCH_TRIAGE_SHADOW", "1") == "0":
        print("triage shadow: disabled by ORCH_TRIAGE_SHADOW=0")
        return 0
    try:
        items = json.loads(args.snapshot.read_text()) if args.snapshot else discover_candidates()
        proposal = json.loads(args.proposal_json.read_text()) if args.proposal_json else None
        row = record_cycle(items, dispatch=proposal is None, proposal_json=proposal)
        if args.backfill:
            row["backfill"] = outcomes.backfill_triage_disagreements()
        print(json.dumps(row, sort_keys=True))
        return int(bool(row["errors"]))
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"shadow": True, "error": str(exc), "population": "UNKNOWN"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
