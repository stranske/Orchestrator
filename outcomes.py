#!/usr/bin/env python3
"""outcomes.py — close the feedback loop: ingest delegated PR outcomes (gate #3).

After the orchestrator applies agent:<X> (dispatcher.delegate_remote, mode=remote), the GitHub keepalive
runs that agent and the PR evolves. This reads each such run's real PR state (gh) and records the OUTCOME
by PR — merged / abandoned / still-pending — joining the DECISION to its RESULT so the learner finally
gets LIVE data instead of only the A/B/C/D seed. A merge is recorded with durability='pending'; a later
durability pass (3b) downgrades merges that get reverted/reworked/reopened. The un-gameable label
(durability, not green CI) lives in feedback.py.

Local delegates target issues, not PRs, so `--mode local` resolves the deterministic
`orchestrator/issue-N` branch back to the PR state. If that branch never produced a PR and the target
issue is already closed, the local run is terminal and records an abandoned outcome instead of staying a
permanent no-PR join gap. `--selftest` runs fully offline (mocked PR states + temp store).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

import feedback
import provision


def _pr_state(target: str, agent: str | None = None) -> dict | None:
    """Live: gh PR state for owner/repo#N.

    Remote opener delegation applies an agent label to an issue, not an already
    existing PR. In that case the recorded target number is an issue number; if
    direct PR lookup fails, resolve the expected remote keepalive branch.
    """
    repo, num = provision.parse_target(target)
    if num is None:
        return {"lookup_status": "invalid_target", "target": target}
    r = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            str(num),
            "-R",
            repo,
            "--json",
            "number,title,url,headRefName,state,mergedAt,closedAt",
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        direct_failure = {
            "lookup_status": "lookup_failed",
            "target": target,
            "error": (r.stderr or r.stdout or "").strip()[:500],
        }
        remote_issue = _remote_issue_pr_state(repo, num, agent, direct_failure)
        return remote_issue or direct_failure
    try:
        return json.loads(r.stdout)
    except Exception:
        return {"lookup_status": "parse_failed", "target": target}


def _pr_list_by_head(repo: str, branch: str) -> dict | None:
    r = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "-R",
            repo,
            "--head",
            branch,
            "--state",
            "all",
            "--json",
            "number,title,url,headRefName,state,mergedAt,closedAt",
            "--limit",
            "1",
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return {
            "lookup_status": "lookup_failed",
            "branch": branch,
            "error": (r.stderr or r.stdout or "").strip()[:500],
        }
    try:
        arr = json.loads(r.stdout)
    except Exception:
        return {"lookup_status": "parse_failed", "branch": branch}
    if not arr:
        return None
    arr[0]["lookup_status"] = "found"
    arr[0]["branch"] = branch
    return arr[0]


def _remote_issue_pr_state(
    repo: str, num: int, agent: str | None, direct_failure: dict
) -> dict | None:
    """Resolve remote issue-target delegation to the PR created by keepalive.

    dispatcher.delegate_remote can label a ready issue. The remote keepalive
    opener then creates branches such as codex/issue-123, so outcome ingest must
    not permanently treat the original issue number as a missing PR number.
    """
    candidate_branches: list[str] = []
    if agent:
        candidate_branches.append(f"{agent}/issue-{num}")
    for fallback_agent in ("codex", "cursor", "claude", "gemini"):
        candidate_branches.append(f"{fallback_agent}/issue-{num}")
    candidate_branches.append(f"orchestrator/issue-{num}")
    seen: set[str] = set()
    for branch in candidate_branches:
        if branch in seen:
            continue
        seen.add(branch)
        pr = _pr_list_by_head(repo, branch)
        if pr and pr.get("lookup_status") == "found":
            pr["target"] = f"{repo}#{num}"
            pr["direct_lookup_error"] = direct_failure.get("error")
            return pr
        if isinstance(pr, dict) and pr.get("lookup_status") == "lookup_failed":
            continue

    terminal_issue = _closed_issue_without_branch_pr(
        repo, num, next(iter(seen), f"{agent or 'agent'}/issue-{num}")
    )
    if terminal_issue:
        terminal_issue["lookup_status"] = "closed_issue_no_remote_pr"
        terminal_issue["candidateBranches"] = sorted(seen)
        terminal_issue["direct_lookup_error"] = direct_failure.get("error")
        return terminal_issue
    return {
        "lookup_status": "no_pr_for_remote_issue_branch",
        "target": f"{repo}#{num}",
        "candidateBranches": sorted(seen),
        "direct_lookup_error": direct_failure.get("error"),
    }


def _pr_view(repo: str, num: int) -> dict | None:
    r = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            str(num),
            "-R",
            repo,
            "--json",
            "number,title,url,headRefName,state,mergedAt,closedAt",
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return None
    try:
        pr = json.loads(r.stdout)
    except Exception:
        return {"lookup_status": "parse_failed", "target": f"{repo}#{num}"}
    pr["lookup_status"] = "found"
    pr["direct_target_pr"] = True
    return pr


def _local_candidate_branches(num: int, agent: str | None = None) -> list[str]:
    candidates = [f"orchestrator/issue-{num}"]
    if agent:
        candidates.append(f"{agent}/issue-{num}")
    candidates.extend(
        f"{fallback_agent}/issue-{num}"
        for fallback_agent in ("codex", "cursor", "vibe", "gemini", "claude")
    )
    seen: set[str] = set()
    result: list[str] = []
    for branch in candidates:
        if branch in seen:
            continue
        seen.add(branch)
        result.append(branch)
    return result


def _local_pr_state(target: str, agent: str | None = None) -> dict | None:
    """Live: a LOCAL delegate's target is an ISSUE (owner/repo#N); its agent opened a PR on the
    deterministic branch orchestrator/issue-N (provision.py). Resolve that PR's state (most recent
    if several) so local-agent delegations close the loop the same way remote ones do.
    """
    repo, num = provision.parse_target(target)
    if num is None:
        return {"lookup_status": "invalid_target", "target": target}
    direct_pr = _pr_view(repo, num)
    if direct_pr:
        return direct_pr

    candidates = _local_candidate_branches(num, agent)
    first_failure: dict | None = None
    for branch in candidates:
        pr = _pr_list_by_head(repo, branch)
        if pr and pr.get("lookup_status") == "found":
            pr["target"] = target
            return pr
        if isinstance(pr, dict) and pr.get("lookup_status") == "lookup_failed":
            first_failure = pr

    if first_failure:
        first_failure["target"] = target
        first_failure["candidateBranches"] = candidates
        return first_failure
    terminal_issue = _closed_issue_without_branch_pr(repo, num, candidates[0])
    if terminal_issue:
        terminal_issue["candidateBranches"] = candidates
        return terminal_issue
    return {
        "lookup_status": "no_pr_for_branch",
        "target": target,
        "branch": candidates[0],
        "candidateBranches": candidates,
    }


def _closed_issue_without_branch_pr(repo: str, num: int, branch: str) -> dict | None:
    """When a local delegate never opened its deterministic PR branch but the issue is now closed,
    the run is terminal: no future PR state can arrive for that branch. Treat it as abandoned so the
    outcome gap does not remain permanently actionable.
    """
    r = subprocess.run(
        [
            "gh",
            "issue",
            "view",
            str(num),
            "-R",
            repo,
            "--json",
            "number,title,url,state,closedAt,closedByPullRequestsReferences",
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return None
    try:
        issue = json.loads(r.stdout)
    except Exception:
        return None
    if (issue.get("state") or "").upper() != "CLOSED":
        return None
    return {
        "lookup_status": "closed_issue_no_branch_pr",
        "target": f"{repo}#{num}",
        "branch": branch,
        "state": "CLOSED",
        "number": issue.get("number"),
        "title": issue.get("title"),
        "url": issue.get("url"),
        "closedAt": issue.get("closedAt"),
        "closing_pr_count": len(issue.get("closedByPullRequestsReferences") or []),
    }


def state_to_outcome(pr: dict | None) -> dict | None:
    """Pure: map a gh PR state -> feedback.record_outcome kwargs. OPEN -> None (still pending, re-check
    later). MERGED -> success with durability='pending' (a later sweep confirms it actually held).
    CLOSED-unmerged -> abandoned failure."""
    if not pr:
        return None
    st = (pr.get("state") or "").upper()
    if st == "MERGED" or pr.get("mergedAt"):
        return {
            "merged": True,
            "adjudicated_verdict": "PASS",
            "durability": "pending",
            "notes": "remote keepalive PR merged; durability pending sweep",
        }
    if st == "CLOSED":
        if pr.get("lookup_status") in {
            "closed_issue_no_branch_pr",
            "closed_issue_no_remote_pr",
        }:
            closing_pr_count = pr.get("closing_pr_count") or 0
            suffix = (
                f"; closing_pr_count={closing_pr_count}"
                if closing_pr_count
                else "; no closing PR references"
            )
            kind = (
                "remote delegate"
                if pr.get("lookup_status") == "closed_issue_no_remote_pr"
                else "local delegate"
            )
            notes = f"{kind} issue closed without matching branch PR{suffix}"
        else:
            notes = "remote keepalive PR closed unmerged"
        return {
            "merged": False,
            "adjudicated_verdict": "FAIL",
            "durability": "abandoned",
            "notes": notes,
        }
    return None


def _skip_reason(pr: dict | None) -> str:
    if not pr:
        return "state_unavailable"
    status = pr.get("lookup_status")
    if status and status != "found":
        return status
    st = (pr.get("state") or "").upper()
    if st == "OPEN":
        return "open_pr"
    if st:
        return f"unresolved_pr_state:{st.lower()}"
    return "state_unavailable"


def _skip_detail(run: dict, pr: dict | None) -> dict:
    detail = {
        "run_id": run["run_id"],
        "target": run["target"],
        "reason": _skip_reason(pr),
    }
    if not pr:
        return detail
    for key in (
        "lookup_status",
        "state",
        "number",
        "url",
        "headRefName",
        "branch",
        "candidateBranches",
        "direct_target_pr",
        "title",
        "error",
        "direct_lookup_error",
    ):
        if pr.get(key):
            detail[key] = pr[key]
    return detail


def _pending_runs(mode: str) -> list[dict]:
    with feedback._conn() as c:
        if mode != "local":
            rows = c.execute(
                "SELECT r.run_id, r.target, r.agent, r.pr_number, "
                "o.run_id IS NOT NULL, COALESCE(o.durability,'') "
                "FROM runs r LEFT JOIN outcomes o ON r.run_id=o.run_id "
                "WHERE (o.run_id IS NULL OR o.durability='pending') AND r.mode=?",
                (mode,),
            ).fetchall()
        else:
            # Older local delegates recorded their agent mode ("composer"/"full"/"cheap") in
            # runs.mode before stable local mode existed, and some cheap rows predate source
            # stamping. They still target an issue and resolve through the same
            # orchestrator/issue-N PR branch.
            rows = c.execute(
                "SELECT r.run_id, r.target, r.agent, r.pr_number, "
                "o.run_id IS NOT NULL, COALESCE(o.durability,'') "
                "FROM runs r LEFT JOIN outcomes o ON r.run_id=o.run_id "
                "WHERE (o.run_id IS NULL OR o.durability='pending') "
                "AND (r.mode='local' OR "
                "(r.mode IN ('composer','full','cheap') AND instr(r.target,'#')>0))"
            ).fetchall()
    return [
        {
            "run_id": rid,
            "target": target,
            "agent": agent,
            "pr_number": pr_number,
            "has_outcome": bool(has_outcome),
            "existing_durability": durability,
        }
        for rid, target, agent, pr_number, has_outcome, durability in rows
    ]


def _pending_durability_detail(run: dict) -> dict:
    return {
        "run_id": run["run_id"],
        "target": run["target"],
        "durability": run.get("existing_durability") or "pending",
    }


def ingest_outcomes(
    mode: str = "remote", dry_run: bool = False, _state_fn=None
) -> dict:
    """For each delegated run lacking a resolved outcome, read its PR state and record the outcome.
    Remote runs may target a direct PR or a labeled opener issue whose keepalive PR branch is
    {agent}/issue-N; LOCAL delegate runs target an ISSUE whose agent opened a PR on branch
    orchestrator/issue-N. `_state_fn` overrides the live gh lookup (tests). Returns a summary;
    idempotent (record_outcome patches)."""
    pending = _pending_runs(mode)
    recorded, skipped = [], []
    pending_durability = []
    for run in pending:
        if run.get("has_outcome") and run.get("existing_durability") == "pending":
            pending_durability.append(_pending_durability_detail(run))
            continue
        if _state_fn:
            pr = _state_fn(run["target"])
        elif mode == "local":
            pr = _local_pr_state(run["target"], run.get("agent"))
        else:
            pr = _pr_state(run["target"], run.get("agent"))
        oc = state_to_outcome(pr)
        if oc is None:
            skipped.append(_skip_detail(run, pr))
            continue
        if not dry_run:
            feedback.record_outcome(run["run_id"], **oc)
        recorded.append(
            {
                "run_id": run["run_id"],
                "merged": oc["merged"],
                "durability": oc["durability"],
            }
        )
    return {
        "mode": mode,
        "pending": len(pending),
        "recorded": len(recorded),
        "skipped": len(skipped),
        "pending_durability": len(pending_durability),
        "details": recorded,
        "skipped_details": skipped,
        "pending_durability_details": pending_durability,
    }


def ingest_modes(
    mode: str = "remote",
    *,
    dry_run: bool = False,
    _state_fns: dict[str, object] | None = None,
) -> dict:
    """Run one or both outcome-ingest paths and return a consistent summary."""
    modes = ["remote", "local"] if mode == "both" else [mode]
    results = []
    for item in modes:
        state_fn = (_state_fns or {}).get(item)
        results.append(ingest_outcomes(mode=item, dry_run=dry_run, _state_fn=state_fn))
    skipped_details = [
        detail for row in results for detail in row.get("skipped_details", [])
    ]
    pending_durability_details = [
        detail
        for row in results
        for detail in row.get("pending_durability_details", [])
    ]
    return {
        "mode": mode,
        "dry_run": dry_run,
        "results": results,
        "pending": sum(row.get("pending", 0) for row in results),
        "recorded": sum(row.get("recorded", 0) for row in results),
        "skipped": sum(row.get("skipped", 0) for row in results),
        "pending_durability": sum(row.get("pending_durability", 0) for row in results),
        "skipped_details": skipped_details,
        "pending_durability_details": pending_durability_details,
    }


def _selftest():
    import tempfile
    from pathlib import Path

    tmp = tempfile.mkdtemp(prefix="outcomes-selftest-")
    feedback.DB_PATH = Path(tmp) / "t.db"
    # pure mapping
    assert state_to_outcome({"state": "MERGED"})["merged"] is True
    assert state_to_outcome({"state": "MERGED"})["durability"] == "pending"
    assert (
        state_to_outcome({"state": "CLOSED"})["merged"] is False
        and state_to_outcome({"state": "CLOSED"})["durability"] == "abandoned"
    )
    assert (
        state_to_outcome({"state": "OPEN"}) is None and state_to_outcome(None) is None
    )
    assert _local_candidate_branches(9, "codex") == [
        "orchestrator/issue-9",
        "codex/issue-9",
        "cursor/issue-9",
        "vibe/issue-9",
        "gemini/issue-9",
        "claude/issue-9",
    ]
    # end-to-end: two remote runs, one merged one open -> merged recorded, open skipped
    feedback.record_run(
        "remote:o/r#1:cursor", "o/r#1", "implement", "cursor", mode="remote"
    )
    feedback.record_run(
        "remote:o/r#2:codex", "o/r#2", "implement", "codex", mode="remote"
    )
    states = {"o/r#1": {"state": "MERGED"}, "o/r#2": {"state": "OPEN"}}
    res = ingest_outcomes(mode="remote", _state_fn=lambda t: states.get(t))
    assert res["recorded"] == 1 and res["skipped"] == 1, res
    assert res["skipped_details"][0]["reason"] == "open_pr", res
    assert res["skipped_details"][0]["run_id"] == "remote:o/r#2:codex", res
    missing_pr = _skip_detail(
        {"run_id": "local-missing-pr", "target": "o/r#9"},
        {
            "lookup_status": "no_pr_for_branch",
            "branch": "orchestrator/issue-9",
        },
    )
    assert missing_pr["reason"] == "no_pr_for_branch", missing_pr
    closed_no_branch = state_to_outcome(
        {
            "lookup_status": "closed_issue_no_branch_pr",
            "state": "CLOSED",
            "closing_pr_count": 0,
        }
    )
    assert closed_no_branch["merged"] is False, closed_no_branch
    assert closed_no_branch["durability"] == "abandoned", closed_no_branch
    assert "without matching branch PR" in closed_no_branch["notes"]
    with feedback._conn() as c:
        row = c.execute(
            "SELECT merged, durability FROM outcomes WHERE run_id='remote:o/r#1:cursor'"
        ).fetchone()
    assert (
        row and row[0] == 1 and row[1] == "pending"
    ), row  # merged recorded, durability pending
    # merged-but-pending stays on the work list for the durability sweep; open stays (no outcome)
    still = {r["run_id"] for r in feedback.runs_needing_outcome("remote")}
    assert "remote:o/r#1:cursor" in still and "remote:o/r#2:codex" in still, still
    second_pass = ingest_outcomes(mode="remote", _state_fn=lambda t: states.get(t))
    assert second_pass["recorded"] == 0, second_pass
    assert second_pass["pending_durability"] == 1, second_pass
    assert (
        second_pass["pending_durability_details"][0]["run_id"] == "remote:o/r#1:cursor"
    ), second_pass
    # local delegate: target is an ISSUE, mode='local'; ingest(mode='local') resolves the PR by branch
    feedback.record_run("o__r_3-codex-123", "o/r#3", "implement", "codex", mode="local")
    res_l = ingest_outcomes(mode="local", _state_fn=lambda t: {"state": "MERGED"})
    assert res_l["recorded"] == 1, res_l  # local delegation's outcome now closes
    assert "o__r_3-codex-123" not in {
        r["run_id"] for r in feedback.runs_needing_outcome("remote")
    }, "local leaked into remote sweep"
    feedback.record_run(
        "legacy-local-composer",
        "o/r#33",
        "mechanical",
        "cursor",
        mode="composer",
        source="orchestrator_local",
    )
    legacy = ingest_outcomes(
        mode="local",
        dry_run=True,
        _state_fn=lambda t: {"state": "CLOSED"} if t == "o/r#33" else None,
    )
    assert any(
        row["run_id"] == "legacy-local-composer" for row in legacy["details"]
    ), legacy
    feedback.record_run(
        "legacy-local-cheap",
        "o/r#35",
        "codemod",
        "codex",
        mode="cheap",
    )
    legacy_cheap = ingest_outcomes(
        mode="local",
        dry_run=True,
        _state_fn=lambda t: {"state": "MERGED"} if t == "o/r#35" else None,
    )
    assert any(row["run_id"] == "legacy-local-cheap" for row in legacy_cheap["details"]), (
        legacy_cheap
    )
    feedback.record_run(
        "local-closed-no-branch",
        "o/r#34",
        "mechanical",
        "cursor",
        mode="local",
    )
    closed_issue = ingest_outcomes(
        mode="local",
        _state_fn=lambda t: (
            {"lookup_status": "closed_issue_no_branch_pr", "state": "CLOSED"}
            if t == "o/r#34"
            else None
        ),
    )
    assert any(
        row["run_id"] == "local-closed-no-branch" and row["durability"] == "abandoned"
        for row in closed_issue["details"]
    ), closed_issue
    with feedback._conn() as c:
        abandoned = c.execute(
            "SELECT merged, durability FROM outcomes WHERE run_id='local-closed-no-branch'"
        ).fetchone()
    assert abandoned and abandoned[0] == 0 and abandoned[1] == "abandoned", abandoned
    feedback.record_run(
        "remote:o/r#4:vibe", "o/r#4", "implement", "vibe", mode="remote"
    )
    feedback.record_run(
        "o__r_5-cursor-456", "o/r#5", "implement", "cursor", mode="local"
    )
    both = ingest_modes(
        mode="both",
        dry_run=True,
        _state_fns={
            "remote": lambda t: {"state": "CLOSED"} if t == "o/r#4" else None,
            "local": lambda t: {"state": "OPEN"} if t == "o/r#5" else None,
        },
    )
    assert both["recorded"] == 1 and both["skipped"] >= 1, both
    assert both["skipped_details"], both
    assert both["pending_durability"] >= 1, both
    assert both["pending_durability_details"], both
    assert [row["mode"] for row in both["results"]] == ["remote", "local"], both
    both_skips = [
        row for result in both["results"] for row in result.get("skipped_details", [])
    ]
    assert any(row["reason"] == "open_pr" for row in both_skips), both
    assert any(row["reason"] == "state_unavailable" for row in both_skips), both
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)
    print(
        "outcomes.py selftest: OK (state->outcome mapping, ingest records merged/abandoned, "
        "open skipped, merged stays pending for durability sweep)"
    )


def main(argv):
    parser = argparse.ArgumentParser(
        description="Ingest delegated PR outcomes into feedback.py."
    )
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--mode",
        choices=["remote", "local", "both"],
        default="remote",
        help="remote resolves direct keepalive PR targets; local resolves orchestrator/issue-N PR branches",
    )
    args = parser.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    print(
        json.dumps(ingest_modes(args.mode, dry_run=args.dry_run), indent=2, default=str)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
