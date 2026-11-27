#!/usr/bin/env python3
"""What did a PR's CI actually test, and is that still what a merge would put on main?

On `pull_request`, ci.yml checks out GitHub's test merge (`refs/pull/N/merge`): the PR head merged into
the base branch AS IT STOOD WHEN THE RUN STARTED. So a green PR run already tested the combination of
the change with main, but only with that main. Measured on Orchestrator#1508 (2026-11-27): its last
PR run tested 7011b0d1 merged into c29e953e; by the merge, main had one more commit (5e39718e, a
workflow-template sync touching none of the PR's files), and the merged tree differed from the tested
one by exactly that commit's files.

Two consumers, one source of truth:

* `freshness` (merge_guard's command-line path): has main moved since the commit the PR's CI tested?
  Commits that are template syncs or collection-floor reconciles AND touch none of the PR's files are
  exempt (they land several times an hour; requiring a fresh run for each would keep a PR waiting
  forever). Any other commit makes the base stale, and merging main into the branch drains it.
* `deploy_reuse` (orch-mirror-sync.sh): a main commit whose tree equals the tree a green PR run tested
  needs no second run before deploy; a collection-floor reconcile on top of such a commit (only
  `.verify-floor.json` changes) does not change that.

Gate contract (latched-gate check, 2026-11-27): the blocking quantity is the non-exempt commits on main
since the tested base; the drain is merging main into the branch (GitHub's update-branch, a merge, never a
rebase), which the guard performs itself while blocked, so the drain runs while the gate is closed; the
measured population (commits on main not in the tested base) is exactly what that merge brings in; fully
drained prints `fresh` with `behind 0`. Anything this module cannot read is `unmeasured`, which never
blocks (fail toward motion) and says why.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Any

EXEMPT_PREFIXES = (
    "chore: sync workflow templates",
    "chore(floor): reconcile the collection floor",
)
FLOOR_FILE = ".verify-floor.json"
MAX_DRIFT_COMMITS = 20
_MERGE_FETCH = re.compile(r"\+([0-9a-f]{40}):refs/remotes/pull/(\d+)/merge")


def _gh(args: list[str], run_fn=subprocess.run) -> str | None:
    try:
        res = run_fn(["gh", *args], capture_output=True, text=True, timeout=120)
    except Exception:
        return None
    if res.returncode != 0:
        return None
    return res.stdout or ""


def _gh_json(args: list[str], run_fn=subprocess.run) -> Any:
    out = _gh(args, run_fn)
    if out is None:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def tested_merge(
    repo: str, head_sha: str, *, workflow: str = "ci.yml", run_fn=subprocess.run
) -> dict | None:
    """The test merge the newest successful `pull_request` run of `workflow` checked out for head_sha:
    {run_id, url, merge_sha, base_sha, tree}, or None when no such run or no readable merge ref."""
    runs = _gh_json(
        [
            "api",
            f"repos/{repo}/actions/workflows/{workflow}/runs"
            f"?head_sha={head_sha}&event=pull_request&status=success&per_page=10",
        ],
        run_fn,
    )
    for run in sorted(
        (runs or {}).get("workflow_runs") or [],
        key=lambda r: r.get("created_at") or "",
        reverse=True,
    ):
        jobs = _gh_json(["api", f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=5"], run_fn)
        for job in (jobs or {}).get("jobs") or []:
            log = _gh(["api", f"repos/{repo}/actions/jobs/{job['id']}/logs"], run_fn)
            match = _MERGE_FETCH.search(log or "")
            if not match:
                continue
            commit = _gh_json(["api", f"repos/{repo}/git/commits/{match.group(1)}"], run_fn)
            parents = [p.get("sha") for p in (commit or {}).get("parents") or []]
            if len(parents) != 2 or parents[1] != head_sha:
                continue
            return {
                "run_id": run["id"],
                "url": run.get("html_url"),
                "merge_sha": match.group(1),
                "base_sha": parents[0],
                "tree": ((commit or {}).get("tree") or {}).get("sha"),
            }
    return None


def _lines(out: str | None) -> set[str] | None:
    return None if out is None else {line for line in out.splitlines() if line.strip()}


def pr_files(repo: str, pr_number: int, run_fn=subprocess.run) -> set[str] | None:
    return _lines(
        _gh(
            ["api", "--paginate", f"repos/{repo}/pulls/{pr_number}/files", "--jq", ".[].filename"],
            run_fn,
        )
    )


def commit_files(repo: str, sha: str, run_fn=subprocess.run) -> set[str] | None:
    return _lines(_gh(["api", f"repos/{repo}/commits/{sha}", "--jq", ".files[].filename"], run_fn))


def _line(fr: dict) -> str:
    state = fr["state"]
    if state == "fresh":
        return (
            f"base: fresh, CI tested this head on {fr['branch']} {fr['tested_base'][:8]}; behind 0"
        )
    if state == "exempt_drift":
        return (
            f"base: behind {fr['behind']}, all template-sync/floor commits disjoint from this PR; "
            f"non-exempt 0, drainable 0"
        )
    if state == "stale":
        return (
            f"base: behind {fr['behind']} since CI tested this head (non-exempt {fr['non_exempt']}, "
            f"drainable {fr['drainable']} by merging {fr['branch']} into the branch)"
        )
    return f"base: unmeasured ({fr.get('reason')}); not blocking"


def freshness(
    repo: str, pr_number: int, head_sha: str, *, branch: str = "main", run_fn=subprocess.run
) -> dict:
    """Has `branch` moved, beyond exempt commits, since the commit this head's PR CI was tested on?"""

    def done(**fields) -> dict:
        fr = {"branch": branch, **fields}
        fr["line"] = _line(fr)
        return fr

    if not head_sha:
        return done(state="unmeasured", reason="no --expected-head")
    tm = tested_merge(repo, head_sha, run_fn=run_fn)
    if not tm:
        return done(
            state="unmeasured",
            reason="no successful pull_request ci.yml run on this head with a readable test merge",
        )
    cmp = _gh_json(["api", f"repos/{repo}/compare/{tm['base_sha']}...{branch}"], run_fn)
    if not isinstance(cmp, dict) or "ahead_by" not in cmp:
        return done(state="unmeasured", reason="compare unreadable", tested_run=tm["url"])
    behind = int(cmp.get("ahead_by") or 0)
    base = {"tested_run": tm["url"], "tested_base": tm["base_sha"], "behind": behind}
    if behind == 0:
        return done(state="fresh", **base, exempt=0, non_exempt=0, drainable=0)
    commits = cmp.get("commits") or []
    files = pr_files(repo, pr_number, run_fn)
    non_exempt: list[dict] = []
    if behind > MAX_DRIFT_COMMITS or len(commits) < behind or files is None:
        non_exempt = [{"sha": None, "why": "too many or unreadable commits"}] * behind
    else:
        for commit in commits:
            sha = commit.get("sha") or ""
            first = ((commit.get("commit") or {}).get("message") or "").splitlines()[:1]
            subject = first[0] if first else ""
            if not subject.startswith(EXEMPT_PREFIXES):
                non_exempt.append({"sha": sha[:8], "why": subject[:80]})
                continue
            touched = commit_files(repo, sha, run_fn)
            if touched is None or touched & files:
                why = (
                    "files unreadable"
                    if touched is None
                    else f"touches {sorted(touched & files)[:3]}"
                )
                non_exempt.append({"sha": sha[:8], "why": why})
    k = len(non_exempt)
    return done(
        state="stale" if k else "exempt_drift",
        **base,
        exempt=behind - k,
        non_exempt=k,
        drainable=k,
        non_exempt_commits=non_exempt[:10],
    )


def update_branch(repo: str, pr_number: int, head_sha: str, run_fn=subprocess.run) -> dict:
    """Merge the base branch into the PR branch (GitHub update-branch: a merge, never a rebase)."""
    out = _gh(
        [
            "api",
            "-X",
            "PUT",
            f"repos/{repo}/pulls/{pr_number}/update-branch",
            "-f",
            f"expected_head_sha={head_sha}",
        ],
        run_fn,
    )
    return {"ok": out is not None, "detail": (out or "update-branch failed")[:300]}


def deploy_reuse(repo: str, sha: str, *, run_fn=subprocess.run) -> dict:
    """May main commit `sha` deploy on a green PR run instead of its own main run? Yes when its tree is
    the tree that run tested, or when it is a floor reconcile (only FLOOR_FILE) on top of such a commit.
    """
    commit = _gh_json(["api", f"repos/{repo}/commits/{sha}"], run_fn)
    if not isinstance(commit, dict):
        return {"reuse": False, "reason": "commit unreadable"}
    candidates = [(sha, commit, "")]
    files = {f.get("filename") for f in commit.get("files") or []}
    parents = commit.get("parents") or []
    if files == {FLOOR_FILE} and len(parents) == 1:
        parent = _gh_json(["api", f"repos/{repo}/commits/{parents[0]['sha']}"], run_fn)
        if isinstance(parent, dict):
            candidates.append((parents[0]["sha"], parent, f"; {sha[:8]} changes only {FLOOR_FILE}"))
    for cand_sha, cand, note in candidates:
        prs = _gh_json(["api", f"repos/{repo}/commits/{cand_sha}/pulls"], run_fn) or []
        for pr in prs:
            if pr.get("merge_commit_sha") != cand_sha or not pr.get("merged_at"):
                continue
            tm = tested_merge(repo, (pr.get("head") or {}).get("sha") or "", run_fn=run_fn)
            tree = ((cand.get("commit") or {}).get("tree") or {}).get("sha")
            if tm and tm["tree"] and tm["tree"] == tree:
                return {
                    "reuse": True,
                    "url": tm["url"],
                    "detail": f"{cand_sha[:8]} has the tree PR #{pr.get('number')}'s CI tested{note}",
                }
    return {"reuse": False, "reason": "no green PR run tested this tree"}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("freshness")
    f.add_argument("--repo", required=True)
    f.add_argument("--pr", type=int, required=True)
    f.add_argument("--head", required=True)
    d = sub.add_parser("deploy-reuse")
    d.add_argument("--repo", required=True)
    d.add_argument("--sha", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "freshness":
        fr = freshness(args.repo, args.pr, args.head)
        print(json.dumps(fr))
        return 1 if fr["state"] == "stale" else 0
    out = deploy_reuse(args.repo, args.sha)
    print(json.dumps(out))
    return 0 if out["reuse"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
