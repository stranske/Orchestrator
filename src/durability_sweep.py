#!/usr/bin/env python3
"""durability_sweep.py - resolve pending durability labels for merged orchestrator outcomes.

A merge is only a provisional success, and a provisional success still SCORES:
`feedback._is_success('pending', 'PASS')` is True, so every merged row this sweep leaves pending
counts as a win until it is judged. Days after the merge, the sweep finds THE merge each pending
row recorded (`find_merge`) and judges whether it held: durable, reverted, reopened, abandoned
(delivered nothing) or broke_later. broke_later is read from EVERY fix PR merged since the merge,
one read per repo (`read_fix_window`); a merge that read did not reach is never durable, it waits
(`DRAIN_FIX_SEARCH`) and is closed unchecked if no run reaches it in time. A row whose merge cannot
be identified as the run's own is closed as `unjudgeable` and excluded from learning: never left
pending, never called a failure.
Every row it does leave pending names what will judge it (`DRAINS`), and every run prints how many
rows are pending, how many of them something will drain, and how many nothing will.

`--selftest` runs fully offline against a temp feedback store.
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
from typing import Any
from urllib.parse import quote

import feedback
import outcomes
import provision
import verifier_evidence

GRACE_DAYS = 7
REVERT_WINDOW_DAYS = (
    21  # reverts land within days of a merge; bound the base-commit scan to this window
)
SECONDS_PER_DAY = 86400
MAX_REVERT_PRS = 50
# THE FIX-PR READ (broke_later): one search per repo per run, bounded by merge date, read WHOLE.
# Until 2026-10-04 it was one unbounded `--limit 200`, and GitHub orders an unsorted search by
# relevance, so a repo with more fix PRs than that (Workflows 1,185 all-time, Trend_Model_Project
# 538) got 200 drawn from its whole history, and a merge's own weeks were read only by chance:
# 436 rows were judged durable that way. GitHub returns at most this many results to one query.
FIX_SEARCH_CAP = 1000
# A window holding FIX_SEARCH_CAP or more is split by merge date and its halves read newest first.
# The budget bounds a split that cannot converge, so it can never become a search per PR.
FIX_SEARCH_MAX_READS = 8
# Narrower than this a window is not split again: FIX_SEARCH_CAP fix PRs merged inside one minute
# is not a busy repository, it is a query GitHub did not apply.
FIX_SEARCH_MIN_SPLIT_S = 60
# A merge the read did not reach stays pending, re-read by every run, for at most this long from
# the first run that missed it; then it is closed as feedback.BROKE_LATER_UNCHECKED. Counted per
# row in FIX_RETRY_STATE, never from the merge, so a backlog row's first failed read cannot close it.
FIX_SEARCH_RETRY_DAYS = 7
FIX_RETRY_STATE = "durability-fix-search-retry.json"  # under $ORCH_STATE_DIR
MAX_BASE_COMMITS = 100
EXPLICIT_MERGED_PR_RE = re.compile(r"\bPR\s+#(?P<num>\d+)\s+merged\b", re.IGNORECASE)
# Every field classify_durability reads, so a direct PR target costs ONE `gh pr view`.
PR_FIELDS = "number,state,mergedAt,closedAt,mergeCommit,baseRefName,headRefName,title,files"
# What GitHub prints when a number is not a pull request: here, an ISSUE target. The only failure
# text the resolver reads; any other failure means GitHub did not answer, never "no PR".
GH_NOT_A_PULL_REQUEST = "Could not resolve to a PullRequest"
# PRs listed per own branch. A branch carrying this many cannot be read whole, so its merge is
# ambiguous rather than guessed.
OWN_BRANCH_PR_LIMIT = 30
# A merge an outcome recorded landed before ingest wrote the row; this allows for the local clock
# running behind GitHub's.
INGEST_CLOCK_SKEW_S = 3600

# What judges a row the sweep leaves pending. A pending row is BLOCKING: it still scores as a
# provisional PASS. It is DRAINABLE only when one of these will judge it; anything else is
# reported as undrainable, which a correct sweep never produces.
DRAIN_GRACE = "grace"  # the merge is younger than GRACE_DAYS: judged on a known date
DRAIN_RETRY = "retry"  # GitHub did not answer this run: the next run asks again
# The fix-PR read did not reach the merge: re-read next run, closed unchecked on a known date.
DRAIN_FIX_SEARCH = "fix_search"
DRAIN_ACTING_RUN = "acting_run"  # a role run: its verdict propagates when its acting run's lands
DRAINS = (DRAIN_GRACE, DRAIN_RETRY, DRAIN_FIX_SEARCH, DRAIN_ACTING_RUN)
VERDICTS = ("durable", "reverted", "reopened", "abandoned", "broke_later")

# The revert check's notes that mean nothing answered, so a later run may get an answer. Any other
# note on an undecided check means it answered and still cannot decide (both searches past their
# limits), which no rerun changes: that row is closed as unjudgeable instead of skipped forever.
REVERT_SEARCH_FAILED = "revert PR search failed"
BASE_SCAN_FAILED = "base commit scan failed"
BASE_BRANCH_UNKNOWN = "missing base branch"
REVERT_RESOLVER_AMBIGUOUS = "revert resolver ambiguous"
UNANSWERED_REVERT_NOTES = frozenset(
    {REVERT_SEARCH_FAILED, BASE_SCAN_FAILED, BASE_BRANCH_UNKNOWN, REVERT_RESOLVER_AMBIGUOUS}
)


def _pending_merged_runs() -> list[dict]:
    """Runs whose outcome is a merged PR but durability has not been resolved yet."""
    with feedback._conn() as c:
        rows = c.execute(
            "SELECT r.run_id, r.target, r.mode, r.pr_number, COALESCE(o.notes,''), r.agent, r.ts, "
            "o.durability_checked_ts "
            "FROM runs r JOIN outcomes o ON r.run_id=o.run_id "
            "WHERE o.merged=1 AND COALESCE(o.durability,'pending')='pending' "
            "ORDER BY r.ts ASC"
        ).fetchall()
    return [
        {
            "run_id": rid,
            "target": target,
            "mode": mode,
            "pr_number": prn,
            "notes": notes,
            "agent": agent,
            "ts": ts,
            # When ingest recorded the merge: the merge it saw cannot have landed later than this.
            "recorded_ts": recorded,
        }
        for rid, target, mode, prn, notes, agent, ts, recorded in rows
    ]


def _acting_runs(run_id: str) -> list[tuple[str, str]]:
    """(acting run, its durability) for each run this one influenced as an ACCEPTED role.

    A role run is not judged here. `feedback` copies the acting run's outcome onto it, durability
    and an excluded class included, whenever that run's outcome is recorded. A role run's target is
    a role label such as `triage:4-items`, so the sweep could never look it up: the three that were
    pending sat skipped for "missing or invalid mergedAt" on every run since August.
    """
    with feedback._conn() as c:
        return [
            (str(target), str(durability or "pending"))
            for target, durability in c.execute(
                "SELECT e.target_run_id, o.durability FROM influence_edges e "
                "LEFT JOIN outcomes o ON o.run_id=e.target_run_id "
                "WHERE e.source_run_id=? AND e.influence_type='role' AND e.accepted=1 "
                "ORDER BY e.created_ts, e.edge_id",
                (run_id,),
            )
        ]


def _current_durability(run_id: str) -> str:
    with feedback._conn() as c:
        row = c.execute("SELECT durability FROM outcomes WHERE run_id=?", (run_id,)).fetchone()
    return str((row[0] if row else None) or "pending")


def _merged_verifier_candidates() -> dict[tuple[str, int], set[str]]:
    """Resolve every merged outcome to an explicit PR before attributing evidence."""
    with feedback._conn() as c:
        rows = c.execute(
            "SELECT r.run_id,r.target,r.source,COALESCE(o.notes,'') "
            "FROM runs r JOIN outcomes o ON r.run_id=o.run_id "
            "WHERE o.merged=1 ORDER BY r.ts ASC"
        ).fetchall()
    found: dict[tuple[str, int], set[str]] = {}
    for run_id, target, source, notes in rows:
        repo, number = provision.parse_target(str(target or ""))
        if not repo:
            continue
        if source != "keepalive":
            explicit = _explicit_merged_pr_target(str(target or ""), notes)
            if not explicit:
                continue
            repo, number = provision.parse_target(explicit)
        if number is not None:
            found.setdefault((repo.lower(), int(number)), set()).add(str(run_id))
    return found


def verifier_candidate_run_ids(
    repo: str, number: int, *, prospective_run_id: str | None = None
) -> set[str]:
    """A PR marker can credit one run only when its Brain mapping is unique."""
    candidates = _merged_verifier_candidates().get((repo.lower(), int(number)), set()).copy()
    if prospective_run_id:
        candidates.add(prospective_run_id)
    return candidates


def _missing_verifier_runs() -> list[tuple[str, str, int]]:
    """Include already-durable rows: verification often arrives after merge."""
    candidates = _merged_verifier_candidates()
    with feedback._conn() as c:
        missing = {
            row[0]
            for row in c.execute("SELECT run_id FROM outcomes WHERE verifier_verdict IS NULL")
        }
    return [
        (run_id, repo, number)
        for (repo, number), run_ids in candidates.items()
        if len(run_ids) == 1
        for run_id in run_ids
        if run_id in missing
    ]


def refresh_verifier_verdicts(*, dry_run: bool = False, _fetch_fn=None) -> dict:
    """Patch exact-merge verifier evidence without changing durability state."""
    fetch = _fetch_fn or verifier_evidence.fetch_decisions
    rows = _missing_verifier_runs()
    by_repo: dict[str, set[int]] = {}
    for _run_id, repo, number in rows:
        by_repo.setdefault(repo, set()).add(number)
    decisions = {
        (repo, number): decision
        for repo, numbers in by_repo.items()
        for number, decision in fetch(repo, sorted(numbers)).items()
    }
    recorded = 0
    for run_id, repo, number in rows:
        decision = decisions.get((repo, number))
        if not isinstance(decision, dict) or decision.get("verdict") not in {"PASS", "NON_PASS"}:
            continue
        if not dry_run:
            feedback.record_outcome(run_id, verifier_verdict=decision["verdict"])
        recorded += 1
    return {"verifier_checked": len(rows), "verifier_recorded": recorded}


def _explicit_merged_pr_target(target: str, notes: str | None) -> str | None:
    repo, _num = provision.parse_target(target)
    if not repo:
        return None
    match = EXPLICIT_MERGED_PR_RE.search(notes or "")
    if not match:
        return None
    return f"{repo}#{int(match.group('num'))}"


def _state_lookup_target(run: dict) -> str:
    """Use an explicitly recorded merged PR when the run target was the source issue."""
    return _explicit_merged_pr_target(run["target"], run.get("notes")) or run["target"]


def _parse_gh_ts(value: str | None) -> int | None:
    if not value:
        return None
    try:
        dt = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return None


def _age_days(ts: int, now: int) -> int:
    return max(0, int((now - ts) // SECONDS_PER_DAY))


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


def _gh_throttle(resource: str) -> None:
    """Pace/defer against the shared GitHub rate budget (gh_capacity) when ORCH_GH_THROTTLE=1;
    no-op + fail-open otherwise so the sweep never breaks on a missing/erroring module."""
    try:
        import gh_capacity

        gh_capacity.throttle_if_enabled(resource)
    except Exception:
        pass


def _gh_answer(args: list[str], *, timeout: int = 30) -> tuple[object | None, str | None]:
    """(parsed JSON, None) when gh answered; (None, what went wrong) when it did not."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "gh timed out"
    if r.returncode != 0:
        return None, (r.stderr or r.stdout or "").strip()[:300] or f"gh exited {r.returncode}"
    try:
        return json.loads(r.stdout), None
    except Exception:
        return None, "gh printed no JSON"


def _own_branch(run: dict, number: int) -> str | None:
    """The branch THIS run's own work landed on, when its target is an issue.

    A local delegate works on `orchestrator/issue-N` (provision.provision_worktree). A remote one
    labels the issue `agent:<X>` and keepalive opens `<X>/issue-N`. Exact names only: widening to
    `<X>/issue-N-*` was measured on 2026-10-04 and would credit other runs' PRs (#411).
    """
    if outcomes.is_local_delegate(run.get("mode"), run.get("target")):
        return f"orchestrator/issue-{number}"
    agent = str(run.get("agent") or "").strip()
    return f"{agent}/issue-{number}" if agent else None


def _found(pr: dict, repo: str, how: str) -> dict:
    return {"status": "found", "pr": {**pr, "repo": pr.get("repo") or repo}, "how": how}


def _unanswered(reason: str) -> dict:
    return {"status": "unanswered", "reason": reason}


def _unjudgeable(reason: str) -> dict:
    return {"status": "unjudgeable", "reason": reason}


def _pr_record(obj: object) -> dict | None:
    """A PR record GitHub answered with, or None. A record needs a state to be an answer."""
    return obj if isinstance(obj, dict) and isinstance(obj.get("state"), str) else None


def find_merge(run: dict, *, _gh=None, now: int | None = None) -> dict:
    """THE merge this merged outcome recorded: found exactly, or a definite reason it cannot be.

    Outcome ingest never stored which PR it credited. `runs.pr_number` is NULL for a local delegate
    and holds the ISSUE number for a remote one, and the ingest note says only "PR merged". So the
    merge is found again here, and it must be the SAME merge or none: a durable verdict on another
    run's merge is a false success the learner cannot see.

    Three answers, and only the first leads to a verdict:
    - found: the target is itself a PR (or the outcome notes name "PR #N merged") and it merged;
      or the target is an issue and exactly ONE merge on the run's own branch landed after the run
      started and no later than its outcome was recorded.
    - unanswered: GitHub did not answer. The next sweep asks again.
    - unjudgeable: GitHub answered and no such merge exists. No rerun changes that.

    Until 2026-10-04 the sweep fetched merge details by the TARGET number, which for an issue
    target is not a PR, and took the newest PR on a branch rather than the merged one. 101 rows sat
    skipped for a merge SHA the sweep never asked for, and 10 for a merge time it never found.
    """
    gh = _gh or _gh_answer
    target = _state_lookup_target(run)
    named_in_notes = target != run["target"]
    repo, number = provision.parse_target(str(target or ""))
    if not repo or number is None:
        return _unjudgeable(
            f"no repository target ({run['target']!r}) and no acting run to inherit a verdict from"
        )
    _gh_throttle("graphql")
    answer, error = gh(["gh", "pr", "view", str(number), "-R", repo, "--json", PR_FIELDS])
    pr = _pr_record(answer)
    if pr is not None:
        how = "named in the outcome notes" if named_in_notes else "direct PR target"
        if pr["state"].upper() == "MERGED" or pr.get("mergedAt"):
            return _found(pr, repo, how)
        return _unjudgeable(
            f"the row recorded a merge, and PR #{number} ({how}) reads {pr['state']} with no merge"
        )
    if answer is not None or not error or GH_NOT_A_PULL_REQUEST not in error:
        return _unanswered(
            f"PR lookup for {repo}#{number} did not answer: {error or 'no PR record'}"
        )
    if named_in_notes:
        return _unjudgeable(f"the outcome notes name PR #{number}, and it is not a pull request")
    branch = _own_branch(run, number)
    if branch is None:
        return _unjudgeable(f"{repo}#{number} is an issue and no agent names the run's own branch")
    _gh_throttle("graphql")
    listed, error = gh(
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
            "number,state,mergedAt",
            "--limit",
            str(OWN_BRANCH_PR_LIMIT),
        ]
    )
    prs = [_pr_record(item) for item in listed] if isinstance(listed, list) else None
    if prs is None or any(item is None for item in prs):
        return _unanswered(f"PR list for {repo} {branch} did not answer: {error or 'no PR list'}")
    records = [item for item in prs if item is not None]
    if len(records) >= OWN_BRANCH_PR_LIMIT:
        return _unjudgeable(f"{branch} carries {len(records)}+ PRs; its merge cannot be read whole")
    started = int(run.get("ts") or 0)
    recorded = int(run.get("recorded_ts") or now or time.time()) + INGEST_CLOCK_SKEW_S
    merges = [item for item in records if item["state"].upper() == "MERGED"]
    in_window = [
        item for item in merges if started <= (_parse_gh_ts(item.get("mergedAt")) or -1) <= recorded
    ]
    if len(in_window) == 1:
        _gh_throttle("graphql")
        merged = int(in_window[0]["number"])
        answer, error = gh(["gh", "pr", "view", str(merged), "-R", repo, "--json", PR_FIELDS])
        detail = _pr_record(answer)
        if detail is None:
            return _unanswered(f"PR #{merged} on {branch} did not answer: {error or 'no record'}")
        return _found(detail, repo, f"the one merge on {branch}")
    names = ", ".join(f"#{item.get('number')} at {item.get('mergedAt')}" for item in merges)
    if in_window:
        return _unjudgeable(
            f"{len(in_window)} merges on {branch} fall in the run's window ({names})"
        )
    if merges:
        return _unjudgeable(
            f"the merges on {branch} ({names}) land before the run started or after its outcome "
            "was recorded; the merge this row recorded is not the run's own"
        )
    if records:
        return _unjudgeable(
            f"{branch} has {len(records)} PR(s) and none merged; the merge this row recorded is "
            "another branch's"
        )
    return _unjudgeable(f"no PR on {branch}; the merge this row recorded is another branch's")


def _merge_sha(pr: dict) -> str | None:
    mc = pr.get("mergeCommit")
    if isinstance(mc, dict):
        return mc.get("oid") or mc.get("sha")
    if isinstance(mc, str):
        return mc
    return pr.get("mergeCommitOid") or pr.get("merge_sha") or pr.get("mergeCommitSha")


def _default_branch(repo: str) -> str | None:
    try:
        r = subprocess.run(
            [
                "gh",
                "repo",
                "view",
                repo,
                "--json",
                "defaultBranchRef",
                "-q",
                ".defaultBranchRef.name",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _contains_ref(text: str, number: int) -> bool:
    return re.search(rf"(?<!\d)#{number}(?!\d)", text or "") is not None


def _fetch_repo_revert_prs(repo: str) -> tuple[list | None, bool]:
    """Repo-wide 'Revert in:title' SEARCH (rate: REST search = 30/min).

    The result is identical for every PR in the repo, so callers cache it per repo and
    match locally — a bulk sweep over N PRs in a repo then costs ONE search, not N.
    Returns (merged_revert_prs, hit_limit); the list is None on search failure.
    """
    _gh_throttle("search")  # SEARCH = 30/min, the binding constraint on bulk sweeps
    arr = _run_json(
        [
            "gh",
            "pr",
            "list",
            "-R",
            repo,
            "--state",
            "merged",
            "--search",
            "Revert in:title",
            "--json",
            "number,title,body",
            "--limit",
            str(MAX_REVERT_PRS),
        ]
    )
    if not isinstance(arr, list):
        return None, False
    return arr, len(arr) >= MAX_REVERT_PRS


def _search_ts(ts: int) -> str:
    """A moment in GitHub search syntax: UTC, to the second."""
    return _dt.datetime.fromtimestamp(int(ts), _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch_repo_fix_prs(
    repo: str, since_ts: int, until_ts: int | None = None
) -> tuple[list | None, bool]:
    """ONE search: the fix-titled PRs merged in [since, until], open-ended when until is None.

    Returns (merged_fix_prs, complete); the list is None when the search did not answer. Complete
    means fewer than FIX_SEARCH_CAP came back, which is the whole window. The window is ONE range
    qualifier on purpose: GitHub does not AND two `merged:` qualifiers (`merged:>=A merged:<B`
    returned the repository's all-time count, 1,185, on 2026-10-04).
    """
    _gh_throttle("search")  # SEARCH = 30/min, the binding constraint on bulk sweeps
    until = "*" if until_ts is None else _search_ts(until_ts)
    arr = _run_json(
        [
            "gh",
            "pr",
            "list",
            "-R",
            repo,
            "--state",
            "merged",
            "--search",
            f"fix in:title merged:{_search_ts(since_ts)}..{until}",
            "--json",
            "number,title,body,mergedAt",
            "--limit",
            str(FIX_SEARCH_CAP),
        ],
        timeout=180,
    )
    if not isinstance(arr, list):
        return None, False
    return arr, len(arr) < FIX_SEARCH_CAP


def read_fix_window(repo: str, since_ts: int, *, now: int, _fetch=None) -> dict:
    """Every fix PR merged from `since_ts` to now, read whole as far back as the search allows.

    `covered_from` is the earliest moment from which EVERY fix PR merged up to now was read, and
    `_fix_covers` is the one test of a merge against it. Windows are read newest first, so a read
    that fails costs only the merges older than it; None means not even the newest one was read.
    """
    fetch = _fetch or _fetch_repo_fix_prs
    entry: dict[str, Any] = {
        "since": int(since_ts),
        "covered_from": None,
        "items": {},
        "reads": 0,
        "error": None,
    }
    windows: list[tuple[int, int | None]] = [(int(since_ts), None)]  # a stack, newest on top
    while windows:
        start, end = windows.pop()
        if entry["reads"] >= FIX_SEARCH_MAX_READS:
            entry["error"] = f"fix-PR search spent its {FIX_SEARCH_MAX_READS} reads"
            break
        arr, complete = fetch(repo, start, end)
        entry["reads"] += 1
        if arr is None:
            entry["error"] = "fix-PR search unavailable"
            break
        if not complete:
            stop = now if end is None else end
            if stop - start <= FIX_SEARCH_MIN_SPLIT_S:
                entry["error"] = f"{FIX_SEARCH_CAP}+ fix PRs read as merged within {stop - start}s"
                break
            middle = (start + stop) // 2
            windows += [(start, middle), (middle, end)]  # the newer half is read first
            continue
        entry["items"].update({item.get("number"): item for item in arr if isinstance(item, dict)})
        entry["covered_from"] = start
    return entry


def _fix_covers(entry: dict | None, merged_ts: int) -> bool:
    """Was every fix PR merged after `merged_ts` read? ONE test, for the verdict and the report."""
    covered_from = (entry or {}).get("covered_from")
    return covered_from is not None and covered_from <= merged_ts


def _fix_followup_status(
    repo: str,
    pr_number: int,
    merged_ts: int,
    fix_cache: dict | None = None,
    _fix_fn=None,
    *,
    since_ts: int | None = None,
    now: int | None = None,
) -> tuple[bool | None, str]:
    """Did a LATER merged fix PR explicitly NAME this one? That is `broke_later`.

    A HIGH BAR, deliberately, and higher than the one escaped_defect_priority uses. That module
    ranks work on the loose signal — a fix commit touching the same file — which is decent
    evidence for ORDERING and poor evidence for TRAINING, because code is changed for many
    reasons and a fix on the same file may repair something this change never touched. This
    writes `outcomes.durability`, which the router learns from, so it demands an explicit
    reference: a fix-titled PR that names this PR number and merged AFTER it.

    READ WHOLE, OR UNKNOWN. False means every fix PR merged after this merge was read and none
    named it. Anything less is None, and None is never durable.
    Until 2026-10-04 it was: an "additive" rule let an incomplete search fall through to the old
    verdict so that no row could be stranded pending, and a search cut at 200 best matches left
    the merge's own weeks unread for 436 durable rows. The stranding is now bounded in the sweep
    (FIX_SEARCH_RETRY_DAYS), so unknown can stay unknown without becoming a latch.

    `since_ts` is the oldest merge the caller will ask about in this repo, so ONE read covers them
    all (`_fix_search_plan`); `fix_cache` holds that read for the run.
    """
    now = int(now or time.time())
    entry = fix_cache.get(repo) if fix_cache is not None else None
    if entry is None:
        start = merged_ts if since_ts is None else min(since_ts, merged_ts)
        entry = read_fix_window(repo, start, now=now, _fetch=_fix_fn)
        if fix_cache is not None:
            fix_cache[repo] = entry
    if not _fix_covers(entry, merged_ts):
        reached = entry["covered_from"]
        return None, (
            f"fix-PR search did not reach this merge ({entry['error'] or 'window starts later'}; "
            f"read whole back to {_search_ts(reached) if reached is not None else 'nothing'})"
        )
    for item in sorted(entry["items"].values(), key=lambda item: str(item.get("mergedAt") or "")):
        number = item.get("number")
        if number == pr_number:
            continue  # a change cannot be the follow-up that reports its own breakage
        later = _parse_gh_ts(item.get("mergedAt"))
        if later is None or later <= merged_ts:
            continue  # a fix that landed FIRST cannot be repairing this
        title = item.get("title") or ""
        haystack = f"{title}\n{item.get('body') or ''}"
        if _contains_ref(haystack, pr_number):
            return True, f"later fix PR #{number} names this change"
    return False, (
        "no later fix PR names this change "
        f"(read every fix PR merged since {_search_ts(entry['covered_from'])})"
    )


def _revert_pr_status(
    repo: str, pr_number: int, revert_cache: dict | None = None
) -> tuple[bool | None, str]:
    """Was `pr_number` reverted by a merged 'Revert ...' PR? Matches `pr_number` locally
    against the repo-wide revert search, cached per repo when `revert_cache` is supplied
    (the per-PR search otherwise hit the 30/min REST search limit on bulk sweeps)."""
    if revert_cache is not None and repo in revert_cache:
        arr, hit_limit = revert_cache[repo]
    else:
        arr, hit_limit = _fetch_repo_revert_prs(repo)
        if revert_cache is not None:
            # Cache even a failed search (arr is None): one search attempt per repo per run,
            # then fall back to the CORE-limited commit scan per PR rather than re-hitting search.
            revert_cache[repo] = (arr, hit_limit)
    if arr is None:
        return None, REVERT_SEARCH_FAILED
    for item in arr:
        title = item.get("title") or ""
        haystack = f"{title}\n{item.get('body') or ''}"
        if title.lower().startswith("revert") and _contains_ref(haystack, pr_number):
            return True, f"revert PR found: #{item.get('number')}"
    if hit_limit:
        return None, f"revert PR search hit limit {MAX_REVERT_PRS}"
    return False, "no revert PR found"


def _revert_commit_status(
    repo: str, base: str, merge_sha: str, merged_at: str
) -> tuple[bool | None, str]:
    # Bound the scan to a window AFTER the merge: a commit two months later is not "this merge failed",
    # and scanning every commit since an old merge always blows past MAX_BASE_COMMITS in active repos
    # (which forced every old merged PR to stay pending — the keepalive backfill exposed this).
    until_param = ""
    merged_ts = _parse_gh_ts(merged_at)
    if merged_ts is not None:
        until = _dt.datetime.fromtimestamp(
            merged_ts + REVERT_WINDOW_DAYS * SECONDS_PER_DAY, _dt.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        until_param = f"&until={quote(until, safe='')}"
    path = (
        f"repos/{repo}/commits?sha={quote(base, safe='')}"
        f"&since={quote(merged_at, safe='')}{until_param}&per_page={MAX_BASE_COMMITS}"
    )
    _gh_throttle("core")  # gh api = CORE (5000/hr)
    arr = _run_json(["gh", "api", path])
    if not isinstance(arr, list):
        return None, BASE_SCAN_FAILED
    needle = f"This reverts commit {merge_sha}"
    for item in arr:
        msg = (item.get("commit") or {}).get("message") or ""
        if needle in msg:
            sha = item.get("sha") or "unknown"
            return True, f"revert commit found: {sha}"
    if len(arr) >= MAX_BASE_COMMITS:
        return None, f"base commit scan hit limit {MAX_BASE_COMMITS}"
    return False, "no base-branch revert commit found"


def _live_revert_status(pr: dict, revert_cache: dict | None = None) -> tuple[bool | None, str]:
    repo = pr.get("repo")
    pr_number = pr.get("number") or pr.get("pr_number")
    merge_sha = _merge_sha(pr)
    merged_at = pr.get("mergedAt")
    base = pr.get("baseRefName")

    if not repo:
        return None, "missing repo"

    pr_check: tuple[bool | None, str] = (None, "missing PR number")
    if pr_number is not None:
        pr_check = _revert_pr_status(repo, int(pr_number), revert_cache=revert_cache)
        if pr_check[0] is True:
            return pr_check

    # The commit scan needs the merge SHA, the merge time and the base branch, and it is only ever
    # a second opinion: a COMPLETED revert-PR search that found nothing answers on its own (below).
    # So what the scan lacks costs the scan alone. Until 2026-10-04 a missing SHA returned "unknown"
    # here before the search's answer was read.
    commit_check: tuple[bool | None, str]
    if not merge_sha:
        commit_check = (None, "missing merge commit SHA")
    elif not merged_at:
        commit_check = (None, "missing mergedAt")
    else:
        base = base or _default_branch(repo)
        if base:
            commit_check = _revert_commit_status(repo, base, merge_sha, merged_at)
        else:
            commit_check = (None, BASE_BRANCH_UNKNOWN)
    if commit_check[0] is True:
        return commit_check
    # No revert found. A completed "no revert PR" probe is sufficient in these PR-gated repos even when
    # the bounded commit scan was inconclusive (a high-churn window can still hit the page limit).
    if pr_check[0] is False or commit_check[0] is False:
        return False, "no revert PR or base-branch revert commit found"
    # Both notes travel, because whether a rerun can decide depends on both (UNANSWERED_REVERT_NOTES).
    return None, f"{pr_check[1]}; {commit_check[1]}"


def _normalize_revert_status(value) -> tuple[bool | None, str]:
    if isinstance(value, tuple) and len(value) == 2:
        return value
    if isinstance(value, dict):
        return value.get("reverted"), value.get("note") or value.get("reason") or "revert resolver"
    if value is True:
        return True, "revert resolver found revert"
    if value is False:
        return False, "revert resolver found no revert"
    return None, REVERT_RESOLVER_AMBIGUOUS


def _revert_drain(note: str) -> str | None:
    """DRAIN_RETRY when part of an undecided revert check went unanswered, else None (closed)."""
    parts = {part.strip() for part in str(note or "").split(";")}
    return DRAIN_RETRY if parts & UNANSWERED_REVERT_NOTES else None


def _revert_status(
    pr: dict, _revert_fn=None, revert_cache: dict | None = None
) -> tuple[bool | None, str]:
    if _revert_fn:
        return _normalize_revert_status(_revert_fn(pr))
    if "reverted" in pr:
        return _normalize_revert_status(pr.get("reverted"))
    return _live_revert_status(pr, revert_cache=revert_cache)


# Paths that are pure agent bookkeeping: touching only these delivers nothing to the product.
# Evidence for the guard (2026-08-18): Counter_Risk#791 "[design-system] Adopt the shared light
# theme" was CLOSED by PR #792 "Agent belt for #791", whose entire diff was one 240-line
# `.agents/issue-791-ledger.yml`. Zero app files. The design-system kit stayed vendored and
# uncalled -- verbatim the failure the tracker existed to fix -- and the run recorded merged=1, so
# the learner would have counted it as a durable success.
#
# Deliberately NARROW. A ledger-only PR is not automatically a false completion: Pension-Data#644
# and learning-management-system#366 were also ledger-only, but sibling PRs delivered the real work,
# so the parent issue was genuinely done. The signal is only trustworthy for the specific run whose
# own merged PR delivered nothing, which is exactly what this classifier is scoped to.
BOOKKEEPING_PATH_RE = re.compile(r"^\.agents/", re.IGNORECASE)


def delivered(pr: dict | None) -> bool | None:
    """Did this merged PR change anything outside agent bookkeeping?

    Returns True (delivered), False (bookkeeping only), or None (cannot tell).

    FAIL-SAFE DIRECTION: None and True both keep the existing durable path. Wrongly calling a real
    delivery "no delivery" would train a capable agent as incapable off a missing API field, which
    is worse than missing one false success -- so only a CONFIDENT, non-empty, all-bookkeeping file
    list downgrades anything.
    """
    if not isinstance(pr, dict) or "files" not in pr:
        return None
    files = pr.get("files")
    if not isinstance(files, list) or not files:
        return None  # no file list is unknown, never "delivered nothing"
    paths = []
    for entry in files:
        path = entry.get("path") if isinstance(entry, dict) else entry
        if path:
            paths.append(str(path))
    if not paths:
        return None
    return not all(BOOKKEEPING_PATH_RE.search(p) for p in paths)


def classify_durability(
    run: dict,
    pr: dict | None,
    *,
    grace_days: int = GRACE_DAYS,
    now: int | None = None,
    _revert_fn=None,
    revert_cache: dict | None = None,
    _fix_fn=None,
    fix_cache: dict | None = None,
    fix_since: dict | None = None,
) -> dict:
    """Pure-ish classifier. An undecided result (`durability` None) names its `drain`: what will
    decide it later (DRAINS), or None when nothing can, which the sweep closes as unjudgeable.
    Wherever the fix check ran, `fix_search` says whether its read reached this merge."""
    now = int(now or time.time())
    if not pr:
        return {"durability": None, "reason": "PR state unavailable", "drain": DRAIN_RETRY}

    repo, target_num = provision.parse_target(run["target"])
    pr = dict(pr)
    pr.setdefault("repo", repo)
    if run.get("mode") != "local":
        pr.setdefault("number", target_num)

    merged_at = pr.get("mergedAt")
    merged_ts = _parse_gh_ts(merged_at)
    if merged_ts is None:
        return {"durability": None, "reason": "missing or invalid mergedAt", "drain": None}

    age = _age_days(merged_ts, now)
    if now - merged_ts < grace_days * SECONDS_PER_DAY:
        return {
            "durability": None,
            "reason": f"merge age {age}d < grace {grace_days}d",
            "drain": DRAIN_GRACE,
            "drains_at": merged_ts + grace_days * SECONDS_PER_DAY,
        }

    state = (pr.get("state") or "").upper()
    if state == "OPEN":
        return {
            "durability": "reopened",
            "notes": f"durability_sweep: PR reopened after merge; merge age {age}d",
        }

    if state not in ("MERGED", "CLOSED"):
        return {
            "durability": None,
            "reason": f"ambiguous PR state {state or 'unknown'}",
            "drain": None,
        }

    reverted, revert_note = _revert_status(pr, _revert_fn=_revert_fn, revert_cache=revert_cache)
    if reverted is True:
        return {
            "durability": "reverted",
            "notes": f"durability_sweep: {revert_note}; merge age {age}d",
        }
    if reverted is None:
        return {"durability": None, "reason": revert_note, "drain": _revert_drain(revert_note)}

    if delivered(pr) is False:
        paths = ", ".join(
            str(f.get("path") if isinstance(f, dict) else f) for f in (pr.get("files") or [])
        )[:160]
        return {
            "durability": "abandoned",
            "notes": (
                f"durability_sweep: merged but delivered nothing -- every changed path is "
                f"agent bookkeeping ({paths}); merge age {age}d"
            ),
        }

    # BROKE_LATER: merged, not reverted, delivered real work -- and then a later fix PR named it.
    # Checked here rather than earlier because a reverted or abandoned change is already
    # classified by a stronger signal, and re-labelling it would lose that.
    fix_repo = pr.get("repo") or repo
    broke, fix_note = _fix_followup_status(
        fix_repo,
        int(pr.get("number") or target_num or 0),
        merged_ts,
        fix_cache=fix_cache,
        _fix_fn=_fix_fn,
        since_ts=(fix_since or {}).get(fix_repo),
        now=now,
    )
    fix_search = "uncovered" if broke is None else "covered"
    if broke is True:
        return {
            "durability": "broke_later",
            "notes": f"durability_sweep: {fix_note}; held {age}d before that",
            "fix_search": fix_search,
        }
    if broke is None:
        # NEVER DURABLE ON AN UNREAD WINDOW. Until 2026-10-04 this fell through to durable. The
        # sweep keeps the row pending and closes it unchecked if no run reaches it in time.
        return {
            "durability": None,
            "reason": fix_note,
            "drain": DRAIN_FIX_SEARCH,
            "fix_search": fix_search,
        }
    return {
        "durability": "durable",
        "notes": f"durability_sweep: held {age}d; {revert_note}; {fix_note}",
        "fix_search": fix_search,
    }


def sweep_durability(
    *,
    grace_days: int = GRACE_DAYS,
    dry_run: bool = False,
    _state_fn=None,
    _revert_fn=None,
    _now: int | None = None,
    _verifier_fetch_fn=None,
    _gh=None,
    _fix_fn=None,
) -> dict:
    """Judge every merged+pending outcome, close the ones that can never be judged, and say what
    will judge each one left pending.

    THE LATCH THIS REPLACED (2026-10-04). The sweep chose every merged pending row but could judge
    only a direct PR target: an issue target's merge commit was fetched by the issue number, the
    newest PR on a branch stood in for the merged one, and a role run has no repository target at
    all. Each run re-skipped the same rows and counted them in one `skipped` number with the rows
    merely waiting out the grace period, so 111 rows waited forever, 27 of them scored as successes
    meanwhile, and the summary read as "busy". Now every row ends one of three ways (judged,
    closed as unjudgeable, or pending with a named drain), and the summary prints the pending count
    beside the drainable one, so a stuck row reads as undrainable on the day it appears.

    Merges are found first and judged second, because the broke-later check reads each repo's fix
    PRs ONCE, from the oldest merge it will be asked about (`_fix_search_plan`); `fix_search` in the
    summary counts the merges that read covered and the ones it did not.
    """
    # Every class classify_durability() can return starts at ZERO here, so the summary always prints
    # them all — a class that never occurred reads as `0`, not as a missing key. And the increment
    # below tolerates a class this table has not learned, because the alternative was measured: the
    # sweep died with `KeyError: 'abandoned'` on every attempt from 2026-08-21 to 2026-09-14 — 71
    # consecutive failures, output discarded by the tick, every merged outcome since then left
    # `pending`, and the router's durability signal silently frozen. A new verdict must never be
    # able to switch the sweep off.
    summary: dict[str, Any] = {
        "checked": 0,
        **{verdict: 0 for verdict in VERDICTS},
        "judged": 0,
        feedback.DURABILITY_UNJUDGEABLE: 0,
        # Rows left pending. BLOCKING: each still scores as a provisional PASS.
        "skipped": 0,
        "drainable": 0,
        "undrainable": 0,
        "drains": {drain: 0 for drain in DRAINS},
        "next_grace_drain": None,
        "next_fix_search_close": None,
        # Merges the broke-later check was asked about: read whole back to the merge (covered) or
        # not (uncovered, of which `closed` were closed unchecked this run), and what it took.
        "fix_search": {"covered": 0, "uncovered": 0, "closed": 0, "reads": 0, "repos": {}},
        "lineage_resolved": 0,
        "skip_reasons": {},
        "details": [],
    }
    # Existing injected PR-state tests are offline; production and explicit verifier tests
    # refresh the verdict independently of durability's grace-period selection.
    if _state_fn is None or _verifier_fetch_fn is not None:
        summary.update(refresh_verifier_verdicts(dry_run=dry_run, _fetch_fn=_verifier_fetch_fn))
    now = int(_now or time.time())
    revert_cache: dict = {}  # repo -> cached revert search, so a bulk sweep does 1 search/repo
    fix_cache: dict = {}  # repo -> its one fix-PR read this run (read_fix_window)
    fix_retry = _load_fix_retry()  # run_id -> first run whose read did not reach its merge
    fix_retry_before = dict(fix_retry)
    role_runs: list[dict] = []
    resolved: list[tuple[dict, dict]] = []
    for run in _pending_merged_runs():
        summary["checked"] += 1
        if _acting_runs(run["run_id"]):
            role_runs.append(run)  # judged after every acting run has had its chance, below
            continue
        if _state_fn is not None:
            state = _state_fn(_state_lookup_target(run))
            found = (
                _found(state, provision.parse_target(run["target"])[0], "injected state")
                if isinstance(state, dict)
                else _unanswered("PR state unavailable")
            )
        else:
            found = find_merge(run, _gh=_gh, now=now)
        if found["status"] == "unanswered":
            _leave_pending(summary, run, found["reason"], DRAIN_RETRY)
            continue
        if found["status"] != "found":
            _close_unjudgeable(summary, run, found["reason"], dry_run=dry_run)
            continue
        resolved.append((run, found))
    fix_since = _fix_search_plan(resolved, now=now, grace_days=grace_days)
    for run, found in resolved:
        verdict = classify_durability(
            run,
            found["pr"],
            grace_days=grace_days,
            now=now,
            _revert_fn=_revert_fn,
            revert_cache=revert_cache,
            _fix_fn=_fix_fn,
            fix_cache=fix_cache,
            fix_since=fix_since,
        )
        if verdict.get("fix_search") in ("covered", "uncovered"):
            summary["fix_search"][verdict["fix_search"]] += 1
        durability = verdict.get("durability")
        if durability is None:
            if verdict.get("drain") == DRAIN_FIX_SEARCH:
                # The read did not reach this merge. Pending, so the next run reads again, until
                # FIX_SEARCH_RETRY_DAYS after the first run that missed it; then closed unchecked.
                unread_since = fix_retry.setdefault(run["run_id"], now)
                closes_at = unread_since + FIX_SEARCH_RETRY_DAYS * SECONDS_PER_DAY
                if now < closes_at:
                    _leave_pending(summary, run, verdict["reason"], DRAIN_FIX_SEARCH, closes_at)
                    continue
                summary["fix_search"]["closed"] += 1
                _close_unjudgeable(
                    summary,
                    run,
                    f"{verdict['reason']}; unread since {_search_ts(unread_since)}",
                    dry_run=dry_run,
                    failure_class=feedback.BROKE_LATER_UNCHECKED,
                )
            elif verdict.get("drain") in DRAINS:
                _leave_pending(
                    summary, run, verdict["reason"], verdict["drain"], verdict.get("drains_at")
                )
            else:
                _close_unjudgeable(summary, run, verdict["reason"], dry_run=dry_run)
            continue

        summary[durability] = summary.get(durability, 0) + 1
        summary["judged"] += 1
        notes = verdict["notes"]
        if found["how"] not in ("direct PR target", "injected state"):
            # The target does not name the PR this verdict is about, so the note does.
            notes += f"; judged #{found['pr'].get('number')} ({found['how']})"
        detail = {
            "run_id": run["run_id"],
            "target": run["target"],
            "action": "patch",
            "durability": durability,
            "notes": notes,
        }
        if dry_run:
            detail["dry_run"] = True
        else:
            feedback.record_outcome(run["run_id"], durability=durability, notes=notes)
        summary["details"].append(detail)
    for run in role_runs:
        acting = _acting_runs(run["run_id"])
        if _current_durability(run["run_id"]) != "pending":
            summary["lineage_resolved"] += 1  # its acting run was judged above and it propagated
            summary["details"].append(
                {"run_id": run["run_id"], "target": run["target"], "action": "lineage"}
            )
            continue
        waiting = [acting_run for acting_run, durability in acting if durability == "pending"]
        if waiting:
            _leave_pending(summary, run, f"awaits its acting run {waiting[0]}", DRAIN_ACTING_RUN)
        else:
            # Every acting run is judged and the verdict never arrived: a propagation defect, not a
            # durability question. Reported, never patched here.
            judged = ", ".join(f"{acting_run}={durability}" for acting_run, durability in acting)
            _leave_pending(summary, run, f"acting runs judged ({judged}) did not propagate", None)
    summary["fix_search"]["reads"] = sum(entry["reads"] for entry in fix_cache.values())
    summary["fix_search"]["repos"] = {
        repo: _fix_read_summary(entry) for repo, entry in sorted(fix_cache.items())
    }
    if not dry_run:
        # A clock lives exactly as long as its row stays pending: judged or closed drops it.
        pending = {d["run_id"] for d in summary["details"] if d.get("action") == "skip"}
        kept = {run_id: ts for run_id, ts in fix_retry.items() if run_id in pending}
        _save_fix_retry(kept, fix_retry_before)
    summary["line"] = summary_line(summary)
    return summary


def _fix_search_plan(
    resolved: list[tuple[dict, dict]], *, now: int, grace_days: int
) -> dict[str, int]:
    """repo -> the oldest merge past grace among the merges about to be judged.

    Only a merge past grace reaches the fix check, so a read from the oldest of them reaches every
    merge the check will be asked about in that repo: ONE window per repo, never one per PR.
    """
    since: dict[str, int] = {}
    for run, found in resolved:
        pr = found["pr"]
        merged_ts = _parse_gh_ts(pr.get("mergedAt"))
        repo = pr.get("repo") or provision.parse_target(run["target"])[0]
        if merged_ts is None or not repo or now - merged_ts < grace_days * SECONDS_PER_DAY:
            continue
        since[repo] = min(since.get(repo, merged_ts), merged_ts)
    return since


def _fix_read_summary(entry: dict) -> dict:
    covered_from = entry["covered_from"]
    return {
        "since": _search_ts(entry["since"]),
        "covered_from": _search_ts(covered_from) if covered_from is not None else None,
        "fix_prs": len(entry["items"]),
        "reads": entry["reads"],
        "error": entry["error"],
    }


def _fix_retry_path() -> Path:
    state_dir = os.environ.get("ORCH_STATE_DIR") or Path.home() / ".codex" / "orchestrator"
    return Path(state_dir) / FIX_RETRY_STATE


def _load_fix_retry() -> dict[str, int]:
    """run_id -> when a run first could not read the fix PRs back to that row's merge.

    Absent or unreadable reads as empty, which restarts every clock: a row is then retried LONGER,
    and still closes FIX_SEARCH_RETRY_DAYS later. Losing this file can never close a row early.
    """
    try:
        data = json.loads(_fix_retry_path().read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(run_id): int(ts)
        for run_id, ts in data.items()
        if isinstance(ts, (int, float)) and not isinstance(ts, bool)
    }


def _save_fix_retry(state: dict[str, int], previous: dict[str, int]) -> None:
    if state == previous:
        return  # a run that moved no clock never touches the state directory
    path = _fix_retry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(path)


def _leave_pending(
    summary: dict, run: dict, reason: str, drain: str | None, drains_at: int | None = None
) -> None:
    """Count a row left pending under the drain that will judge it, or as undrainable."""
    summary["skipped"] += 1
    key = re.sub(r"\d+", "N", str(reason))
    summary["skip_reasons"][key] = summary["skip_reasons"].get(key, 0) + 1
    if drain in DRAINS:
        summary["drainable"] += 1
        summary["drains"][drain] += 1
    else:
        summary["undrainable"] += 1
    if drains_at:
        due = _dt.datetime.fromtimestamp(drains_at, _dt.timezone.utc).strftime("%Y-%m-%d")
        when = "next_fix_search_close" if drain == DRAIN_FIX_SEARCH else "next_grace_drain"
        summary[when] = min(filter(None, (summary[when], due)))
    summary["details"].append(
        {
            "run_id": run["run_id"],
            "target": run["target"],
            "action": "skip",
            "reason": reason,
            "drain": drain,
        }
    )


def _close_unjudgeable(
    summary: dict,
    run: dict,
    reason: str,
    *,
    dry_run: bool,
    failure_class: str = feedback.UNJUDGEABLE_MERGE,
) -> None:
    """Close a row whose merge can never be judged: terminal, excluded from learning, never FAIL."""
    summary[feedback.DURABILITY_UNJUDGEABLE] += 1
    notes = f"durability_sweep: unjudgeable: {reason}"
    detail = {
        "run_id": run["run_id"],
        "target": run["target"],
        "action": "patch",
        "durability": feedback.DURABILITY_UNJUDGEABLE,
        "failure_class": failure_class,
        "notes": notes,
    }
    if dry_run:
        detail["dry_run"] = True
    else:
        feedback.record_outcome(
            run["run_id"],
            durability=feedback.DURABILITY_UNJUDGEABLE,
            failure_class=failure_class,
            notes=notes,
        )
    summary["details"].append(detail)


def summary_line(summary: dict) -> str:
    """One line with the blocking number beside the drainable one, and the drained state named."""
    verdicts = ", ".join(f"{verdict} {summary.get(verdict, 0)}" for verdict in VERDICTS)
    fix = summary["fix_search"]
    head = (
        f"durability_sweep: checked {summary['checked']}; judged {summary['judged']} ({verdicts}); "
        f"closed unjudgeable {summary[feedback.DURABILITY_UNJUDGEABLE]}; "
        f"fix search covered {fix['covered']}, uncovered {fix['uncovered']}"
    )
    if fix["closed"]:
        head += f" ({fix['closed']} closed unchecked)"
    pending = summary["skipped"]
    if pending == 0:
        return f"{head}; pending 0, fully drained"
    drains = ", ".join(f"{drain} {summary['drains'][drain]}" for drain in DRAINS)
    if summary["next_grace_drain"]:
        drains += f", next grace drain {summary['next_grace_drain']}"
    if summary["next_fix_search_close"]:
        drains += f", next unread close {summary['next_fix_search_close']}"
    tail = f"{head}; pending {pending}, drainable {summary['drainable']} ({drains})"
    if summary["undrainable"]:
        stuck = next(d for d in summary["details"] if d.get("action") == "skip" and not d["drain"])
        return f"{tail}, UNDRAINABLE {summary['undrainable']} (first: {stuck['reason']})"
    return f"{tail}, undrainable 0"


def _iso_days_ago(now: int, days: int) -> str:
    dt = _dt.datetime.fromtimestamp(now - days * SECONDS_PER_DAY, _dt.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _selftest_live_revert_scan(now: int):
    """Exercise the live revert resolver without the injected `reverted` shortcut."""
    old = _iso_days_ago(now, 10)

    def classify_with_canned_gh(
        pr_number: int, merge_sha: str, pr_rows: list[dict], commit_rows: list[dict]
    ) -> tuple[dict, list[list[str]]]:
        calls = []
        real_run_json = globals()["_run_json"]

        def fake_run_json(args: list[str], *, timeout: int = 30):
            calls.append(args)
            if args[:3] == ["gh", "pr", "list"]:
                return pr_rows
            if args[:2] == ["gh", "api"]:
                return commit_rows
            raise AssertionError(f"unexpected gh call: {args}")

        globals()["_run_json"] = fake_run_json
        try:
            return (
                classify_durability(
                    {"target": f"o/r#{pr_number}", "mode": "remote"},
                    {
                        "state": "MERGED",
                        "number": pr_number,
                        "mergedAt": old,
                        "baseRefName": "main",
                        "mergeCommit": {"oid": merge_sha},
                    },
                    grace_days=GRACE_DAYS,
                    now=now,
                ),
                calls,
            )
        finally:
            globals()["_run_json"] = real_run_json

    full_page_without_revert = [
        {"sha": f"noise-{i}", "commit": {"message": f"ordinary commit {i}"}}
        for i in range(MAX_BASE_COMMITS)
    ]
    verdict, calls = classify_with_canned_gh(
        101,
        "merge-trust",
        [{"number": 900, "title": "Revert unrelated change", "body": "Refs #999"}],
        full_page_without_revert,
    )
    assert verdict["durability"] == "durable", verdict

    # ---- no-delivery guard: a merge that changed only agent bookkeeping is NOT a durable win ----
    # Modelled on the real case: Counter_Risk#791 closed by PR #792 whose entire diff was
    # `.agents/issue-791-ledger.yml`, so merged=1 would have trained as a success.
    _old = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=GRACE_DAYS + 5)
    _old_iso = _old.strftime("%Y-%m-%dT%H:%M:%SZ")
    _now_ts = int(time.time())

    def _verdict_for(files):
        pr = {
            "state": "MERGED",
            "number": 792,
            "mergedAt": _old_iso,
            "baseRefName": "main",
            "mergeCommit": {"oid": "deadbeef"},
        }
        if files is not None:
            pr["files"] = files
        return classify_durability(
            {"target": "o/r#792", "mode": "remote"},
            pr,
            grace_days=GRACE_DAYS,
            now=_now_ts,
            _revert_fn=lambda *a, **k: (False, "no revert found"),
            _fix_fn=lambda *_window: ([], True),  # every fix PR read, none names it
        )

    ledger_only = _verdict_for([{"path": ".agents/issue-791-ledger.yml"}])
    assert ledger_only["durability"] == "abandoned", ledger_only
    assert "delivered nothing" in ledger_only["notes"], ledger_only
    # It must count as a REGRESSION, never a success, for the capability learner.
    assert "abandoned" in feedback.CAPABILITY_REGRESSION_DURABILITY

    # Real work still resolves durable...
    assert _verdict_for([{"path": "dashboard/theme.py"}])["durability"] == "durable"
    # ...and so does a MIXED diff (a ledger entry alongside real work is normal).
    assert _verdict_for([{"path": ".agents/x.yml"}, {"path": "app.py"}])["durability"] == "durable"

    # FAIL-SAFE: unknowable delivery must never manufacture a failure.
    for unknown in (None, []):  # type: ignore[var-annotated]  # deliberate mixed-type probe
        got = _verdict_for(unknown)
        assert got["durability"] == "durable", (unknown, got)

    # DELIBERATE BREAK -> REVERT: widen the bookkeeping pattern to match everything and real work
    # gets wrongly condemned — proving the pattern is what keeps the guard honest.
    _saved_re = BOOKKEEPING_PATH_RE
    try:
        globals()["BOOKKEEPING_PATH_RE"] = re.compile(r".")
        broken = _verdict_for([{"path": "dashboard/theme.py"}])
        assert (
            broken["durability"] == "abandoned"
        ), "break did not change behaviour — test is vacuous"
    finally:
        globals()["BOOKKEEPING_PATH_RE"] = _saved_re
    assert (
        _verdict_for([{"path": "dashboard/theme.py"}])["durability"] == "durable"
    ), "revert did not restore the guard"

    commit_paths = [args[2] for args in calls if len(args) >= 3 and args[:2] == ["gh", "api"]]
    assert commit_paths, calls
    old_ts = _parse_gh_ts(old)
    assert old_ts is not None, old
    expected_until = _dt.datetime.fromtimestamp(
        old_ts + REVERT_WINDOW_DAYS * SECONDS_PER_DAY, _dt.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    expected_until_param = f"&until={quote(expected_until, safe='')}"
    assert expected_until_param in commit_paths[0], commit_paths[0]

    verdict, _calls = classify_with_canned_gh(
        102,
        "merge-pr-reverted",
        [
            {
                "number": 901,
                "title": "Revert feature from orchestrator",
                "body": "Backs out the change from #102.",
            }
        ],
        [],
    )
    assert verdict["durability"] == "reverted", verdict

    verdict, _calls = classify_with_canned_gh(
        103,
        "merge-commit-reverted",
        [],
        [{"sha": "revert-sha", "commit": {"message": "This reverts commit merge-commit-reverted"}}],
    )
    assert verdict["durability"] == "reverted", verdict


def _selftest_revert_search_cached_per_repo(now: int):
    """A bulk sweep must issue ONE repo-wide revert SEARCH per repo, not one per PR
    (REST search = 30/min; the per-PR search hit a rate wall on the keepalive backfill)."""
    import shutil
    import tempfile

    saved_db = feedback.DB_PATH
    tmp = tempfile.mkdtemp(prefix="durability-sweep-cache-selftest-")
    feedback.DB_PATH = Path(tmp) / "t.db"
    old = _iso_days_ago(now, 10)
    nums = (11, 12, 13)
    try:
        for n in nums:
            rid = f"cache-{n}"
            feedback.record_run(
                rid, f"o/r#{n}", "implement", "codex", mode="remote", ts=now - 20 * SECONDS_PER_DAY
            )
            feedback.record_outcome(
                rid, adjudicated_verdict="PASS", merged=True, durability="pending"
            )
        # PR state withholds the 'reverted' key so classification takes the live search path.
        states = {
            f"o/r#{n}": {
                "state": "MERGED",
                "number": n,
                "mergedAt": old,
                "baseRefName": "main",
                "mergeCommit": {"oid": f"sha-{n}"},
            }
            for n in nums
        }
        search_calls = {"n": 0}
        real_run_json = globals()["_run_json"]

        def fake_run_json(args: list[str], *, timeout: int = 30):
            if args[:3] == ["gh", "pr", "list"] and "--search" in args:
                search_calls["n"] += 1
                return [{"number": 900, "title": "Revert an unrelated PR", "body": "Refs #999"}]
            if args[:2] == ["gh", "api"]:
                return []  # empty base-commit scan -> no revert commit (CORE-limited, per-PR)
            raise AssertionError(f"unexpected gh call: {args}")

        globals()["_run_json"] = fake_run_json
        try:
            res = sweep_durability(
                grace_days=GRACE_DAYS, _state_fn=lambda t: states.get(t), _now=now
            )
        finally:
            globals()["_run_json"] = real_run_json

        # THE INVARIANT IS PER-REPO, NOT A MAGIC NUMBER. Three PRs in one repo cost TWO
        # repo-wide searches — one for reverts, one for fix follow-ups — because both are cached
        # per repo and matched locally. Were either searched per PR this would be 3 or 6, which is
        # the regression this pins; the 30/min REST search limit is what makes it matter.
        assert (
            search_calls["n"] == 2
        ), f"expected 2 repo-wide searches (revert + fix) for 3 PRs, got {search_calls['n']}"
        assert res["checked"] == 3 and res["durable"] == 3, res
    finally:
        feedback.DB_PATH = saved_db
        shutil.rmtree(tmp, ignore_errors=True)


def _selftest_broke_later(now: int):
    """A later fix PR that NAMES this change is broke_later; anything weaker is not.

    The bar is explicit reference on purpose. escaped_defect_priority ranks on the loose signal
    (a fix touching the same file), which is fine for ordering work and wrong for training the
    router — so this asserts the loose case does NOT produce the label.
    """
    pr = {
        "repo": "o/r",
        "number": 10,
        "state": "MERGED",
        "mergedAt": _iso_days_ago(now, 30),
        "files": [{"path": "src/real.py"}],
    }
    run = {"target": "o/r#10", "mode": "remote"}

    def _no_revert(_pr, **_kw):
        return False, "no revert"

    # (a) a later fix PR naming this change -> broke_later
    def _fix_names_it(_repo, _since, _until=None):
        return [
            {
                "number": 11,
                "title": "fix(core): repair regression",
                "body": "Fixes the regression introduced in #10",
                "mergedAt": _iso_days_ago(now, 5),
            }
        ], True

    got = classify_durability(
        run, pr, now=now, _revert_fn=_no_revert, _fix_fn=_fix_names_it, fix_cache={}
    )
    assert got["durability"] == "broke_later", got
    assert "#11" in got["notes"], got

    # (b) a fix that does NOT name it -> durable, not broke_later. Same file is not enough.
    def _fix_unrelated(_repo, _since, _until=None):
        return [
            {
                "number": 12,
                "title": "fix(core): unrelated",
                "body": "no reference at all",
                "mergedAt": _iso_days_ago(now, 5),
            }
        ], True

    got = classify_durability(
        run, pr, now=now, _revert_fn=_no_revert, _fix_fn=_fix_unrelated, fix_cache={}
    )
    assert got["durability"] == "durable", got

    # (c) a fix that landed BEFORE the merge cannot be repairing it
    def _fix_earlier(_repo, _since, _until=None):
        return [
            {
                "number": 13,
                "title": "fix: earlier",
                "body": "Refs #10",
                "mergedAt": _iso_days_ago(now, 60),
            }
        ], True

    got = classify_durability(
        run, pr, now=now, _revert_fn=_no_revert, _fix_fn=_fix_earlier, fix_cache={}
    )
    assert got["durability"] == "durable", got

    # (d) a change cannot be the follow-up that reports its own breakage
    def _fix_is_self(_repo, _since, _until=None):
        return [
            {
                "number": 10,
                "title": "fix: itself",
                "body": "Refs #10",
                "mergedAt": _iso_days_ago(now, 5),
            }
        ], True

    got = classify_durability(
        run, pr, now=now, _revert_fn=_no_revert, _fix_fn=_fix_is_self, fix_cache={}
    )
    assert got["durability"] == "durable", got

    # (e) UNKNOWN IS NOT DURABLE. An unavailable search leaves the merge unread, so it waits under
    # DRAIN_FIX_SEARCH (the sweep bounds the wait); until 2026-10-04 it fell through to durable.
    def _fix_unavailable(_repo, _since, _until=None):
        return None, False

    got = classify_durability(
        run, pr, now=now, _revert_fn=_no_revert, _fix_fn=_fix_unavailable, fix_cache={}
    )
    assert got["durability"] is None and got["drain"] == DRAIN_FIX_SEARCH, got
    assert got["fix_search"] == "uncovered" and "unavailable" in got["reason"], got


def _selftest():
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp(prefix="durability-sweep-selftest-")
    feedback.DB_PATH = Path(tmp) / "t.db"
    saved_state_dir = os.environ.get("ORCH_STATE_DIR")
    os.environ["ORCH_STATE_DIR"] = tmp  # the fix-search retry clocks, never the live ones
    now = int(time.time())
    old = _iso_days_ago(now, 10)
    young = _iso_days_ago(now, 2)

    try:
        cases = [
            (
                "clean",
                "o/r#1",
                {
                    "state": "MERGED",
                    "number": 1,
                    "mergedAt": old,
                    "baseRefName": "main",
                    "mergeCommit": {"oid": "aaa"},
                    "reverted": False,
                },
                "durable",
            ),
            (
                "revert",
                "o/r#2",
                {
                    "state": "MERGED",
                    "number": 2,
                    "mergedAt": old,
                    "baseRefName": "main",
                    "mergeCommit": {"oid": "bbb"},
                    "reverted": True,
                },
                "reverted",
            ),
            (
                "open",
                "o/r#3",
                {
                    "state": "OPEN",
                    "number": 3,
                    "mergedAt": old,
                    "baseRefName": "main",
                    "mergeCommit": {"oid": "ccc"},
                    "reverted": False,
                },
                "reopened",
            ),
            (
                "young",
                "o/r#4",
                {
                    "state": "MERGED",
                    "number": 4,
                    "mergedAt": young,
                    "baseRefName": "main",
                    "mergeCommit": {"oid": "ddd"},
                    "reverted": False,
                },
                "pending",
            ),
            (
                "ambiguous",
                "o/r#5",
                {
                    "state": "MERGED",
                    "number": 5,
                    "mergedAt": old,
                    "baseRefName": "main",
                    "mergeCommit": {"oid": "eee"},
                    "reverted": None,
                },
                "pending",
            ),
            # Merged, but every changed path is agent bookkeeping: `abandoned`. This class was
            # added to classify_durability() without a row in the sweep's summary table, and the
            # first real PR that matched it killed every sweep for 24 days (KeyError: 'abandoned').
            (
                "bookkeeping-only",
                "o/r#7",
                {
                    "state": "MERGED",
                    "number": 7,
                    "mergedAt": old,
                    "baseRefName": "main",
                    "mergeCommit": {"oid": "abc"},
                    "reverted": False,
                    "files": [{"path": ".agents/checkpoints/CHECKPOINT.md"}],
                },
                "abandoned",
            ),
        ]
        states = {}
        for run_id, target, state, _expected in cases:
            feedback.record_run(
                run_id, target, "implement", "codex", mode="remote", ts=now - 20 * SECONDS_PER_DAY
            )
            feedback.record_outcome(
                run_id, adjudicated_verdict="PASS", merged=True, durability="pending"
            )
            states[target] = state

        feedback.record_run(
            "explicit-pr-note",
            "o/r#9",
            "implement",
            "codex",
            mode="full",
            ts=now - 20 * SECONDS_PER_DAY,
        )
        feedback.record_outcome(
            "explicit-pr-note",
            adjudicated_verdict="PASS",
            merged=True,
            durability="pending",
            notes="local delegate completed through PR #10 merged by maintainer",
        )
        states["o/r#10"] = {
            "state": "MERGED",
            "number": 10,
            "mergedAt": old,
            "baseRefName": "main",
            "mergeCommit": {"oid": "fff"},
            "reverted": False,
        }

        res = sweep_durability(
            grace_days=GRACE_DAYS,
            _state_fn=lambda target: states.get(target),
            _now=now,
            _fix_fn=lambda *_window: ([], True),  # every fix PR read, none names a change here
        )
        assert res["checked"] == 7 and res["durable"] == 2 and res["reverted"] == 1, res
        assert res["reopened"] == 1 and res["skipped"] == 2, res
        assert res["abandoned"] == 1 and res["broke_later"] == 0, res

        with feedback._conn() as c:
            got = dict(c.execute("SELECT run_id, durability FROM outcomes").fetchall())
        for run_id, _target, _state, expected in cases:
            assert got[run_id] == expected, (run_id, got, expected)
        assert got["explicit-pr-note"] == "durable", got

        _selftest_live_revert_scan(now)
        _selftest_revert_search_cached_per_repo(now)
        _selftest_broke_later(now)

        print(
            "durability_sweep.py selftest: OK (old clean->durable, revert->reverted, "
            "open->reopened, young/ambiguous stay pending, live revert scan covered, "
            "searches cached per repo not per PR, and broke_later only on an explicit "
            "later-fix reference)"
        )
    finally:
        if saved_state_dir is None:
            os.environ.pop("ORCH_STATE_DIR", None)
        else:
            os.environ["ORCH_STATE_DIR"] = saved_state_dir
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Sweep merged pending outcomes for durability.")
    parser.add_argument("--grace-days", type=int, default=GRACE_DAYS)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)

    if args.selftest:
        _selftest()
        return 0

    res = sweep_durability(grace_days=args.grace_days, dry_run=args.dry_run)
    print(res["line"], file=sys.stderr)
    print(json.dumps(res, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
