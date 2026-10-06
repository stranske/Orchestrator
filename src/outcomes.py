#!/usr/bin/env python3
"""outcomes.py — close the feedback loop: ingest delegated PR outcomes (gate #3).

After the orchestrator applies agent:<X> (dispatcher.delegate_remote, mode=remote), the GitHub keepalive
runs that agent and the PR evolves. This reads each such run's real PR state (gh) and records the OUTCOME
by PR — merged / abandoned / still-pending — joining the DECISION to its RESULT so the learner finally
gets LIVE data instead of only the A/B/C/D seed. A merge is recorded with durability='pending'; a later
durability pass (3b) downgrades merges that get reverted/reworked/reopened. The un-gameable label
(durability, not green CI) lives in feedback.py.

Local delegates target issues, not PRs, so `--mode local` resolves the deterministic
`orchestrator/issue-N` branch back to the PR state. If no candidate branch produced a PR and the target
issue is already closed, the run is terminal instead of staying a permanent no-PR join gap, and the
issue's closing PRs decide which terminal verdict it gets. No closing PR: abandoned, a FAIL. A closing
PR: someone delivered, and no candidate branch says it was this run, so the run records NO verdict and
`feedback.UNATTRIBUTED_CLOSING_PR`, a class no learner scores (counted in the summary's `unattributed`).
A closing PR is a reference that MERGED by the time the issue closed (`_merged_by_close`): GitHub
also lists every PR that links the issue with a closing keyword AFTER it closed, and those are named
in the notes without counting. Until 2026-10-04 any listed reference counted, so an issue closed by
hand weeks before its references existed read as delivered by them (Workflows#2819).
Either verdict needs every candidate branch to have ANSWERED "no PR here", and the merge times of
the issue's references to have been read: a lookup that could not answer leaves the run pending,
retried at the next ingest, and counted in the summary's `unanswered`.

A REMOTE DELEGATION (an `orchestrator_remote` run: the tick applied `agent:<X>` to an issue or PR)
is credited with a PR only when the PR can be shown to be that delegation's work, and three exact
conditions decide it (`_delegated_pr_state`). The run's own PR is on the delegated agent's own
keepalive branch, `{agent}/issue-N`, or is the labelled PR itself: another agent's or another lane's
branch is never this run's. A PR merged or closed before the label was applied cannot be its work.
And a PASS or a FAIL needs at least one COMPLETED round of the delegated agent's keepalive runner
on that PR since the label, not measured unproductive, read from the runner's own trusted markers.
A settled PR without that evidence records no verdict, `feedback.UNATTRIBUTED_DELEGATION`, a class
no learner scores, and so does a delegation whose issue closed with no PR of its own and no closing
PR: its agent runs only on the PR its label bootstraps, so it never ran (owner decision 2026-10-04,
amending #411 for delegations; a local run keeps that FAIL). Until 2026-10-04 the resolver walked every agent's branch and
`orchestrator/issue-N` and credited the first PR found, and a labelled PR's merge went to whatever
agent the label named.

A local run's candidates START with the branches it pushed from its own worktree, which its
completion step read from git's reflogs (`pushed_branches.py`, stored in `feedback.run_pushes`).
Agents name their own branches, so a PR there is the exact answer the name patterns can only guess
at, and it is credited only if it is the PR the run OPENED: head == branch, created at or after the
run started. One that predates the run is rejected and its branch is not asked again. No record, an
unreadable one or an empty one leaves the walk exactly as it was. The summary's `push_records`
counts what happened (`credited`, `rejected`) and what was missing (`absent`, `unreadable`).

A local run's own PR CLOSED unmerged is not yet its FAIL. The opener lane rehomes a PR keepalive
cannot drive: it opens a replacement at the SAME head commit on a registry branch
(`codex/issue-N-<slug>`) and closes the old one, and keepalive finishes the replacement. So before
the FAIL, one read asks which PRs linked to the run's issue carry the closed PR's head commit
(`judge_replacement`). Exactly one that merged after the run started is the run's PASS, named
`replacement PR #M merged` for the durability sweep. None is the FAIL, still: a replacement with
different commits is different work. An open carrier waits, an answer that cannot single one out
is `feedback.UNATTRIBUTED_REPLACEMENT`, and a read that failed is unanswered. Until 2026-10-05 the
closed PR was the FAIL: 5 of the 9 such rows of the 90 days to then, all codex, had merged that day
in a replacement carrying their head. `recheck_replacements` re-judges the rows recorded before
that, once each, behind a snapshot and `--undo-replacement-recheck`. The summary's `replacements`
counts the four answers.
`--selftest` runs fully offline (mocked PR states + temp store).
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import feedback
import provision
import pushed_branches
import utc_epoch

# The PR dict's `credited_via` for a PR found on a branch the run itself pushed (`_pushed_branch_pr`).
PUSHED_BRANCH = "pushed_branch"

# A GitHub lookup here answers one of three ways: it FOUND the thing, it answered that there is
# NOTHING there, or it could not answer (gh failed, or printed something that does not parse). Only
# the second is evidence of absence. Reading the third as the second recorded a closed issue whose
# branch lookups had all failed as `abandoned`: an unknown written into the outcome labels the router
# learns from. So an unanswered lookup ends the resolution, the run is skipped with no outcome row,
# and the next ingest asks again. One set, read by the resolvers and by the summary's count.
UNANSWERED_LOOKUPS = frozenset(
    {
        "lookup_failed",
        "parse_failed",
        "issue_lookup_failed",
        "closing_pr_lookup_failed",
        "runner_rounds_lookup_failed",
        "replacement_lookup_failed",
    }
)
ISSUE_VIEW_STATES = frozenset({"OPEN", "CLOSED", "MERGED"})
# A closed target issue with every candidate branch answered "no PR". Which verdict it gets is decided
# by the issue's closing PRs, in `state_to_outcome`.
CLOSED_ISSUE_LOOKUPS = frozenset({"closed_issue_no_branch_pr", "closed_issue_no_remote_pr"})
# A closing reference counts only if it MERGED at or before the issue closed, plus this many seconds.
# GitHub writes a merge and the close it causes in the same second or the next: over 1,600 closed
# fleet issues (2026-10-04) the PR that closed one merged 0-2 s before its closedAt, once 460 s
# before, never after, and the nearest reference that merged AFTER a close did so 113 s later. ONE
# constant, read only by `_merged_by_close`.
CLOSING_PR_MERGE_SLACK_SECONDS = 10
# The issue's close time and every closing reference with its merge time, in ONE read, so the two
# times compared always come from the same answer and a reference in the list carries its own merge
# time: no second lookup per reference exists to fail. It resolves the number through
# `issueOrPullRequest` and reads `first: 100` with no `includeClosedPrs`, exactly as `gh issue view`
# does (gh 2.94), so it answers whenever the issue view answered; `--paginate` follows `$endCursor`.
CLOSING_PR_QUERY = (
    "query($owner: String!, $name: String!, $number: Int!, $endCursor: String) {"
    " repository(owner: $owner, name: $name) { issueOrPullRequest(number: $number) {"
    " ... on Issue { closedAt closedByPullRequestsReferences(first: 100, after: $endCursor) {"
    " pageInfo { hasNextPage endCursor }"
    " nodes { number url state mergedAt repository { nameWithOwner } } } } } } }"
)
# The verdict classes that mean "terminal, but not this run's work", counted as `unattributed`.
UNATTRIBUTED_CLASSES = frozenset(
    {
        feedback.UNATTRIBUTED_CLOSING_PR,
        feedback.UNATTRIBUTED_DELEGATION,
        feedback.UNATTRIBUTED_REPLACEMENT,
    }
)
# THE REPLACEMENT READ, made only for a LOCAL run's own PR closed unmerged (`_attach_replacement`):
# the closed PR's head commit, and every PR linked to the run's issue with its commits, in ONE
# answer. The opener lane rehomes a PR keepalive cannot drive, such as one on `orchestrator/issue-N`:
# a replacement opens at the SAME head SHA on `codex/issue-N-<slug>` ("Closes #N", "Supersedes
# #old") and the old PR is closed. GitHub lists merged and open links here and leaves closed-unmerged
# ones out, so a replacement abandoned in turn carries nothing. A list as long as the limit may hold
# more than it showed, and is never guessed past (`judge_replacement`). ONE constant for the query
# and the judge. GitHub prices the read at 1 point, the same as `CLOSING_PR_QUERY`.
REPLACEMENT_READ_LIMIT = 100
REPLACEMENT_QUERY = (
    "query($owner: String!, $name: String!, $issue: Int!, $pr: Int!) {"
    " repository(owner: $owner, name: $name) {"
    " pullRequest(number: $pr) { number headRefOid }"
    " issueOrPullRequest(number: $issue) { __typename ... on Issue {"
    " closedByPullRequestsReferences(first: LIMIT) { pageInfo { hasNextPage }"
    " nodes { number state mergedAt headRefName repository { nameWithOwner }"
    " commits(first: LIMIT) { totalCount nodes { commit { oid } } } } } } } } }"
).replace("LIMIT", str(REPLACEMENT_READ_LIMIT))
HEAD_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
LINK_STATES = frozenset({"OPEN", "MERGED", "CLOSED"})
# Written into the notes of every verdict on a local run's closed PR, and into a re-judged row's
# notes by `recheck_replacements`, whose selection skips a row that carries it: ONE string, so the
# re-check drains to a printed zero and stays there.
REPLACEMENT_CHECK = "replacement check"
# How a credited replacement's notes BEGIN. The durability sweep reads the merge to judge from the
# first `PR #N merged` (EXPLICIT_MERGED_PR_RE), and gives a PR's verifier evidence to its own run
# before a run credited this way (`durability_sweep._merged_verifier_candidates`).
REPLACEMENT_CREDIT = f"{REPLACEMENT_CHECK}: replacement PR #"
# `judge_replacement`'s answers -> the summary's `replacements` counts. An unanswered read has no
# key here: it is counted in `unanswered`, like every lookup that could not answer. `not_read` is a
# closed PR the read does not apply to: the run's target is itself a PR, so no issue links to it.
REPLACEMENT_COUNTS = {
    "credited": "credited",
    "open": "waiting",
    "unattributable": "unattributable",
    "none": "failed",
    "not_read": "not_read",
}
# The runs whose PR credit needs the delegation guard: the tick labelled a target for an agent.
# Keepalive-discovered runs (`keepalive`) ARE their PR, so `_pr_state` still resolves them directly.
DELEGATION_SOURCE = "orchestrator_remote"
PR_VIEW_FIELDS = "number,title,url,headRefName,state,mergedAt,closedAt"
# The keepalive runner's own record of each dispatch, a PR comment written by Workflows
# `scripts/runner_lib/core.py`: the legacy `runner-dispatch` record (one per provider, rewritten in
# place, so it holds that provider's latest dispatch), the append-only `runner-reservation` written
# before every dispatch, and the `runner-completion` receipt bound to a reservation by its id.
RUNNER_MARKER_RE = re.compile(
    r"<!--\s*(runner-dispatch|runner-reservation|runner-completion):([\w.-]+):(\d+):v1"
    r"\s+([\s\S]*?)\s*-->"
)
RUNNER_RECEIPT_SCHEMA = "runner-completion-receipt/v1"
# Who may write those markers: runner_lib's `_is_trusted_marker_comment`, a login in this set or an
# author association below. Its fixture-only fallback (no author metadata at all) is not mirrored:
# a real comment always carries a user, and a record nobody can be shown to have written is no
# evidence.
TRUSTED_RUNNER_MARKER_AUTHORS = frozenset(
    {
        "chatgpt-codex-connector",
        "chatgpt-codex-connector[bot]",
        "github-actions[bot]",
        "stranske",
        "stranske-automation-bot",
    }
)
TRUSTED_RUNNER_MARKER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
# Older local delegates recorded their agent mode ("composer"/"full"/"cheap") in runs.mode before the
# stable "local" mode existed, and some cheap rows predate source stamping. They still target an
# issue and resolve through the same orchestrator/issue-N PR branch. ONE definition: local ingest
# selects by it and the durability sweep names a run's own branch by it. Until 2026-10-04 the sweep
# kept its own `mode == "local"`, so it searched these 71 runs' branches in the remote order, and for
# 4 of them it found a closed PR the ingest had never credited.
LEGACY_LOCAL_MODES = ("composer", "full", "cheap")


def is_local_delegate(mode: str | None, target: str | None) -> bool:
    """Did a LOCAL delegate record this run? Its own work lands on orchestrator/issue-N."""
    return mode == "local" or (mode in LEGACY_LOCAL_MODES and "#" in str(target or ""))


def needs_delegation_guard(source: str | None) -> bool:
    """May only `_delegated_pr_state` decide this run's PR credit? True for a remote delegation.

    ONE predicate, two callers. Ingest routes such a run through the guard, and merge_guard never
    credits one, because ingest does not re-decide a run already recorded as merged and pending
    durability. Until 2026-10-04 merge_guard credited the latest remote run on the merged target,
    delegations included, and that PASS bypassed the guard for good."""
    return source == DELEGATION_SOURCE


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
        timeout=60,
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
            "number,title,url,headRefName,state,mergedAt,closedAt,createdAt",
            "--limit",
            "1",
        ],
        capture_output=True,
        text=True,
        timeout=60,
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
    # None is reserved for GitHub answering "no PR has this head": only an empty LIST says that.
    # `{}` or `null` is falsy too, and reading it as an answer would be the same unknown-as-no.
    # Likewise "found" needs a PR record with a state; `[{}]` is no more an answer than `{}`.
    if not isinstance(arr, list) or (
        arr and not (isinstance(arr[0], dict) and isinstance(arr[0].get("state"), str))
    ):
        return {"lookup_status": "parse_failed", "branch": branch}
    if not arr:
        return None
    arr[0]["lookup_status"] = "found"
    arr[0]["branch"] = branch
    return arr[0]


def _branch_pr_lookup(repo: str, branches: list[str]) -> dict | None:
    """Ask each candidate branch in order; return the first answer that is not "no PR here".

    That answer is a FOUND PR or a lookup that could not answer, and either one ends the walk: a PR
    found after an unanswered branch might not be the preferred branch's PR, and an all-clear after
    it would read the unanswered branch as empty. None means every candidate branch answered "no PR
    here", which is the only result the closed-issue abandonment verdict may be built on.
    """
    for branch in branches:
        answer = _pr_list_by_head(repo, branch)
        if answer is not None:
            return answer
    return None


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
    branches = list(dict.fromkeys(candidate_branches))
    pr = _branch_pr_lookup(repo, branches)
    if pr is not None:
        # FOUND: its state decides. Unanswered: it carries no state, so the run is skipped.
        pr["target"] = f"{repo}#{num}"
        pr["candidateBranches"] = sorted(branches)
        pr["direct_lookup_error"] = direct_failure.get("error")
        return pr

    terminal_issue = _closed_issue_without_branch_pr(repo, num, branches[0])
    if terminal_issue is not None:
        if terminal_issue["lookup_status"] == "closed_issue_no_branch_pr":
            terminal_issue["lookup_status"] = "closed_issue_no_remote_pr"
        terminal_issue["candidateBranches"] = sorted(branches)
        terminal_issue["direct_lookup_error"] = direct_failure.get("error")
        return terminal_issue
    return {
        "lookup_status": "no_pr_for_remote_issue_branch",
        "target": f"{repo}#{num}",
        "candidateBranches": sorted(branches),
        "direct_lookup_error": direct_failure.get("error"),
    }


def _iso(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _settled_before(pr: dict, started_ts: int) -> tuple[str, int] | None:
    """Pure: ("merged" | "closed", when) if the PR settled before `started_ts`, else None. A PR that
    was already merged or closed when the label was applied cannot be the labelled agent's work."""
    for key, verb in (("mergedAt", "merged"), ("closedAt", "closed")):
        when = utc_epoch.from_iso(pr.get(key)) if pr.get(key) else None
        if when is not None and when < started_ts:
            return verb, when
    return None


def _trusted_runner_comment(comment: object) -> bool:
    if not isinstance(comment, dict):
        return False
    user = comment.get("user")
    login = user.get("login") if isinstance(user, dict) else None
    if isinstance(login, str) and login.strip().lower() in TRUSTED_RUNNER_MARKER_AUTHORS:
        return True
    association = str(comment.get("author_association") or "").upper()
    return association in TRUSTED_RUNNER_MARKER_ASSOCIATIONS


def _runner_payload(raw: str) -> dict | None:
    value = raw.strip()
    try:
        if value.startswith("base64:"):
            value = base64.b64decode(value.removeprefix("base64:"), validate=True).decode("utf-8")
        payload = json.loads(value)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def runner_records(comments: list, *, provider: str, pr_number: int) -> list[dict]:
    """Resolve trusted reservations, completion receipts and legacy dispatch records.

    Comments must be complete and oldest first, as the REST API lists them. Reuse this parser
    for dispatch provenance so policy joins obey the same identity checks as outcome credit.
    """
    reservations: dict[str, dict] = {}
    receipts: dict[str, dict] = {}
    legacy: dict | None = None
    for comment in comments:
        if not _trusted_runner_comment(comment):
            continue
        for kind, marked, number, raw in RUNNER_MARKER_RE.findall(str(comment.get("body") or "")):
            if marked != provider or int(number) != pr_number:
                continue
            payload = _runner_payload(raw)
            if payload is None:
                continue
            if kind == "runner-completion":
                record, rid = payload.get("record"), payload.get("reservation_id")
                if (
                    payload.get("schema") == RUNNER_RECEIPT_SCHEMA
                    and isinstance(rid, str)
                    and rid
                    and isinstance(record, dict)
                    and record.get("provider") == provider
                    and record.get("pr_number") == pr_number
                    and record.get("reservation_id") == rid
                ):
                    receipts[rid] = record
                continue
            if payload.get("provider") != provider or payload.get("pr_number") != pr_number:
                continue
            if kind == "runner-reservation":
                rid = payload.get("reservation_id")
                if isinstance(rid, str) and rid:
                    reservations[rid] = payload
            else:
                legacy = payload
    rounds = [receipts.get(rid, record) for rid, record in reservations.items()]
    rounds += [record for rid, record in receipts.items() if rid not in reservations]
    if legacy is not None:
        rounds.append(legacy)
    return rounds


def runner_rounds(comments: list, *, provider: str, pr_number: int, since_ts: int) -> dict:
    """Count runner rounds since the label; only completed, productive rounds are credited.

    Missing productivity is unmeasured (and credited for legacy records). Error, pending and
    undated rounds are counted separately and never credited.
    """
    counts = dict.fromkeys(
        ("credited", "unproductive", "errored", "pending", "before_label", "undated"), 0
    )
    for record in runner_records(comments, provider=provider, pr_number=pr_number):
        started = utc_epoch.from_iso(record.get("started_at"))
        if started is None:
            counts["undated"] += 1
        elif started < since_ts:
            counts["before_label"] += 1
        elif record.get("status") == "completed":
            counts["unproductive" if record.get("productive") is False else "credited"] += 1
        elif record.get("status") == "error":
            counts["errored"] += 1
        else:
            counts["pending"] += 1
    return counts


def _runner_rounds(repo: str, pr_number: int, provider: str, since_ts: int) -> dict | None:
    """Live: `runner_rounds` over every comment on the PR. None when GitHub did not answer."""
    r = subprocess.run(
        [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            f"repos/{repo}/issues/{pr_number}/comments?per_page=100",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if r.returncode != 0:
        return None
    try:
        pages = json.loads(r.stdout)
    except Exception:
        return None
    # --slurp wraps the pages in one array, so an answer is a list of lists; anything else is not.
    if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
        return None
    comments = [comment for page in pages for comment in page]
    return runner_rounds(comments, provider=provider, pr_number=pr_number, since_ts=since_ts)


def _attribute_delegation(repo: str, pr: dict, agent: str | None, started_ts: int | None) -> dict:
    """Mark a delegation's candidate PR with whether it can be SHOWN to be the delegation's work.

    An open PR is left unmarked: its verdict waits, and so does the evidence read. A settled one is
    attributable only with a credited round of the delegated agent's runner since the label. A read
    that failed leaves the run unanswered (skipped, retried), never unattributed."""
    pr["delegation"] = True
    state = str(pr.get("state") or "").upper()
    if state == "OPEN" and not pr.get("mergedAt"):
        return pr
    settled = _settled_before(pr, started_ts) if started_ts is not None else None
    if settled is not None and started_ts is not None:
        verb, when = settled
        pr["attribution"] = {
            "attributable": False,
            "reason": f"it {verb} at {_iso(when)}, before the label at {_iso(started_ts)}",
        }
        return pr
    number = pr.get("number")
    if started_ts is None or not agent or not isinstance(number, int):
        pr["attribution"] = {
            "attributable": False,
            "reason": "the run records no delegation time, agent or PR number to check it against",
        }
        return pr
    rounds = _runner_rounds(repo, number, agent, started_ts)
    if rounds is None:
        pr["lookup_status"] = "runner_rounds_lookup_failed"
        return pr
    others = ", ".join(f"{key} {value}" for key, value in rounds.items() if value)
    pr["attribution"] = {
        "attributable": rounds["credited"] > 0,
        "rounds": rounds,
        "reason": (
            f"{agent}'s runner completed {rounds['credited']} credited round(s) on it since the "
            f"label at {_iso(started_ts)}" + (f" ({others})" if others else "")
        ),
    }
    return pr


def _delegated_pr_state(target: str, agent: str | None, started_ts: int | None) -> dict:
    """Live: the PR a remote delegation is credited with, and whether it can be shown to be its.

    That PR is the labelled target itself when the target is a PR. Otherwise it is the PR on the
    delegated agent's own keepalive branch, `{agent}/issue-N`, which the keepalive bootstrap opens
    for that label. No other branch is asked: another agent's branch or the local lane's
    `orchestrator/issue-N` holds that lane's work, and the old first-found walk over them credited
    a gemini label with a local vibe run's PR merged 39 days before the label. An own-branch PR
    settled before the label belongs to an earlier delegation and is passed over too. With no PR of
    its own and the issue closed, the run is over and never this agent's verdict: a closing PR is
    #411's unattributed case, and no closing PR means the labelled agent never had a PR to run on
    (`delegation_without_own_pr`), where a LOCAL run, which did run, keeps #411's FAIL."""
    repo, num = provision.parse_target(target)
    if num is None:
        return {"lookup_status": "invalid_target", "target": target}
    r = subprocess.run(
        ["gh", "pr", "view", str(num), "-R", repo, "--json", PR_VIEW_FIELDS],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if r.returncode == 0:
        try:
            pr = json.loads(r.stdout)
        except Exception:
            pr = None
        if not isinstance(pr, dict) or not isinstance(pr.get("state"), str):
            return {"lookup_status": "parse_failed", "target": target}
        pr.update(lookup_status="found", direct_target_pr=True, target=target)
        return _attribute_delegation(repo, pr, agent, started_ts)
    view_error = (r.stderr or r.stdout or "").strip()[:500]
    own_branch = f"{agent}/issue-{num}" if agent else ""
    context: dict = {
        "target": target,
        "candidateBranches": [own_branch] if own_branch else [],
        "direct_lookup_error": view_error,
    }
    own = _pr_list_by_head(repo, own_branch) if own_branch else None
    if own is not None:
        own.update(context)
        if own.get("lookup_status") != "found":
            return own  # could not answer: it carries no state, so the run is skipped and re-asked
        if started_ts is None or _settled_before(own, started_ts) is None:
            return _attribute_delegation(repo, own, agent, started_ts)
        context["passed_over_pr"] = f"#{own.get('number')}"
    terminal_issue = _closed_issue_without_branch_pr(repo, num, own_branch)
    if terminal_issue is None:
        return {"lookup_status": "no_pr_for_remote_issue_branch", **context}
    if terminal_issue["lookup_status"] == "closed_issue_no_branch_pr":
        if "/pull/" in str(terminal_issue.get("url") or ""):
            # `gh pr view` failed on a PR number, so this is a PR nobody read, not a closed issue.
            return {"lookup_status": "lookup_failed", **context, "error": view_error}
        terminal_issue["lookup_status"] = "closed_issue_no_remote_pr"
        terminal_issue["delegation_without_own_pr"] = True
    terminal_issue.update(context)
    return terminal_issue


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
        timeout=60,
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


def _pushed_branch_pr(
    repo: str, branches: list[str], start_ts: int
) -> tuple[dict | None, list[str]]:
    """Walk the branches the run pushed; return `(answer, rejected)`.

    The answer is the first PR the run OPENED on one of them (`credited_via` == `PUSHED_BRANCH`), or the
    first lookup that could not answer, which skips the run until the next ingest exactly as on any
    candidate branch. A PR record that cannot be checked against the window (no head, no creation
    time) is such a lookup too: unknown is neither credited nor rejected. `rejected` names every PR
    found on a pushed branch that predates the run. The run pushed onto it and did not open it, so
    it is not this run's delivery, and the caller does not ask that branch again.
    """
    rejected: list[str] = []
    for branch in branches:
        answer = _pr_list_by_head(repo, branch)
        if answer is None:
            continue
        if answer.get("lookup_status") != "found":
            return answer, rejected
        opened = pushed_branches.pr_opened_in_window(answer, branch, start_ts)
        if opened is None:
            return {
                "lookup_status": "parse_failed",
                "branch": branch,
                "error": "the PR on a pushed branch has no headRefName or createdAt to check",
            }, rejected
        if opened:
            answer["credited_via"] = PUSHED_BRANCH
            return answer, rejected
        rejected.append(f"#{answer.get('number')} on {branch}")
    return None, rejected


def _unanswered_replacement(error: str) -> dict:
    return {"status": "unanswered", "error": error[:500]}


def _carrier_name(link: dict) -> str:
    cut = "" if link["complete"] else f", commits cut at {REPLACEMENT_READ_LIMIT}"
    when = f" {link['mergedAt']}" if link["state"] == "MERGED" else ""
    return f"#{link['number']} ({link['state'].lower()}{when}{cut})"


def _not_found(answer: dict, field: str) -> bool:
    """Did GitHub answer that `repository.<field>` does not exist? A definite answer, unlike a
    failed read: gh exits 1 for it but still prints the data beside a NOT_FOUND error."""
    errors = answer.get("errors")
    return isinstance(errors, list) and any(
        isinstance(error, dict)
        and error.get("type") == "NOT_FOUND"
        and error.get("path") == ["repository", field]
        for error in errors
    )


def judge_replacement(
    answer: object,
    *,
    repo: str,
    pr_number: int,
    started_ts: int | None,
    opened_ts: int | None,
) -> dict:
    """Pure: does a PR linked to the run's issue carry the head commit of its closed PR #pr_number?

    `answer` is what `REPLACEMENT_QUERY` printed; `opened_ts` is when the closed PR was created. A
    link CARRIES the head when the head is in its commit list; one whose list was cut at
    REPLACEMENT_READ_LIMIT without it MAY. The answers:
    - credited: exactly one MERGED link carries it, merged at or after the run started, and the
      closed PR was opened at or after the run started (#441's window rule: the run opened it). The
      same commit is the same work, in the wrapper the lane rehomed it into: the run's delivery.
    - open: none merged, and an OPEN link carries it or may. The work is in flight, so the run waits
      exactly as on its own open PR, until that PR settles.
    - unattributable: the complete answer cannot single one out: more than one merged link carries
      it or may, the only one that may is unread past the limit, the links run past the limit, or
      the run or its closed PR cannot be placed in time (no start, no creation time, or a PR opened
      before the run started, so not shown to be the run's). Terminal, and scored by no learner.
    - none: no link carries it after the run started, which is the FAIL; so is an issue GitHub
      answers does not exist. A link that carries it but merged BEFORE the run started is named and
      never credited: that head predates the run.
    - not_read: the run's target number is a pull request, so no issue's links exist to read.
    - unanswered: no answer: the read failed, or printed a shape this does not know.
    """
    if not isinstance(answer, dict):
        return _unanswered_replacement("the replacement read printed no answer")
    data = answer.get("data")
    repository = data.get("repository") if isinstance(data, dict) else None
    if not isinstance(repository, dict):
        return _unanswered_replacement("the replacement read printed no repository")
    pull = repository.get("pullRequest")
    head = pull.get("headRefOid") if isinstance(pull, dict) else None
    if not (
        isinstance(pull, dict)
        and pull.get("number") == pr_number
        and isinstance(head, str)
        and HEAD_SHA_RE.match(head)
    ):
        return _unanswered_replacement(f"the closed PR #{pr_number}'s head is not in the answer")
    verdict: dict = {"head": head}
    issue = repository.get("issueOrPullRequest")
    if issue is None and _not_found(answer, "issueOrPullRequest"):
        return {**verdict, "status": "none", "before_run": [], "links": [], "gone": True}
    kind = issue.get("__typename") if isinstance(issue, dict) else None
    if kind == "PullRequest":
        return {**verdict, "status": "not_read"}
    conn = issue.get("closedByPullRequestsReferences") if isinstance(issue, dict) else None
    nodes = conn.get("nodes") if isinstance(conn, dict) else None
    info = conn.get("pageInfo") if isinstance(conn, dict) else None
    more = info.get("hasNextPage") if isinstance(info, dict) else None
    if kind != "Issue" or not isinstance(nodes, list) or not isinstance(more, bool):
        return _unanswered_replacement("the replacement read has no list of the issue's links")
    links: list[dict] = []
    for node in nodes:
        where = node.get("repository") if isinstance(node, dict) else None
        owner_name = where.get("nameWithOwner") if isinstance(where, dict) else None
        state = node.get("state") if isinstance(node, dict) else None
        commits = node.get("commits") if isinstance(node, dict) else None
        oids = commits.get("nodes") if isinstance(commits, dict) else None
        total = commits.get("totalCount") if isinstance(commits, dict) else None
        shas = {
            item["commit"].get("oid")
            for item in oids or []
            if isinstance(item, dict) and isinstance(item.get("commit"), dict)
        }
        if not (
            isinstance(node, dict)
            and isinstance(owner_name, str)
            and isinstance(node.get("number"), int)
            and isinstance(state, str)
            and state.upper() in LINK_STATES
            and isinstance(oids, list)
            and isinstance(total, int)
            and len(shas) == len(oids)
            and all(isinstance(sha, str) for sha in shas)
        ):
            return _unanswered_replacement(f"a link of the issue cannot be read: {node!r}")
        if owner_name.lower() != repo.lower() or node["number"] == pr_number:
            continue  # another repository's PR cannot carry this one's commit; #N is the closed PR
        merged_ts = utc_epoch.from_iso(node.get("mergedAt")) if node.get("mergedAt") else None
        if state.upper() == "MERGED" and merged_ts is None:
            return _unanswered_replacement(f"merged link #{node['number']} has no merge time")
        carries = head in shas
        links.append(
            {
                "number": node["number"],
                "state": state.upper(),
                "mergedAt": node.get("mergedAt"),
                "merged_ts": merged_ts,
                "headRefName": node.get("headRefName"),
                "carries": carries,
                "complete": carries or len(shas) >= total,
            }
        )
    if more:
        return {
            **verdict,
            "status": "unattributable",
            "reason": f"over {REPLACEMENT_READ_LIMIT} PRs link the issue, so any unread one may "
            "carry its head",
        }
    may = [link for link in links if link["carries"] or not link["complete"]]
    merged = [link for link in may if link["state"] == "MERGED"]
    after = [
        link for link in merged if started_ts is None or (link["merged_ts"] or 0) >= started_ts
    ]
    if started_ts is None:
        unplaced = "the run records no start to place the merge against"
    elif opened_ts is None:
        unplaced = f"its closed PR #{pr_number} records no creation time"
    elif opened_ts < started_ts:
        unplaced = f"its closed PR #{pr_number} was opened before the run started"
    else:
        unplaced = ""
    if len(after) == 1 and after[0]["carries"] and not unplaced:
        return {**verdict, "status": "credited", "pr": after[0]}
    if after:
        reason = unplaced or f"{len(after)} merged PR(s) carry its head or may"
        names = ", ".join(_carrier_name(link) for link in after)
        return {**verdict, "status": "unattributable", "reason": f"{reason}: {names}"}
    opened = [link for link in may if link["state"] == "OPEN"]
    if opened:
        return {**verdict, "status": "open", "prs": [_carrier_name(link) for link in opened]}
    return {
        **verdict,
        "status": "none",
        "before_run": [_carrier_name(link) for link in merged if link["carries"]],
        "links": [_carrier_name(link) for link in links],
    }


def _replacement_read(repo: str, issue: int, pr_number: int) -> tuple[object | None, str | None]:
    """Live: `REPLACEMENT_QUERY`, one page. (parsed, None), or (None, why) when gh did not answer.
    A GraphQL error exits gh 1 but still prints the data beside it, and an issue that does not
    resolve is an answer (`_not_found`): the printed data is returned whenever there is some."""
    owner, _, name = repo.partition("/")
    r = subprocess.run(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={REPLACEMENT_QUERY}",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-F",
            f"issue={issue}",
            "-F",
            f"pr={pr_number}",
        ],
        capture_output=True,
        text=True,
    )
    try:
        parsed = json.loads(r.stdout) if r.stdout.strip() else None
    except Exception:
        parsed = None
    if r.returncode == 0 or (isinstance(parsed, dict) and isinstance(parsed.get("data"), dict)):
        if parsed is None:
            return None, "the replacement read printed no JSON"
        return parsed, None
    return None, (r.stderr or r.stdout or "").strip()[:500] or f"gh exited {r.returncode}"


def _attach_replacement(
    pr: dict | None, repo: str, issue: int, started_ts: int | None, *, read: bool = True
):
    """Before a local run's own PR, CLOSED unmerged, becomes its FAIL: the replacement read. The
    verdict rides on the PR as `replacement` for `state_to_outcome`. A read that did not answer, or
    a closed PR record with no number to read by, turns the lookup unanswered, so the run is
    skipped and asked again: never failed on an unknown. `read=False` is a target that is itself
    the PR: no issue links to it, so it is `not_read` without a call. Any other PR state, or no PR,
    passes through untouched."""
    if (
        not isinstance(pr, dict)
        or pr.get("lookup_status") != "found"
        or str(pr.get("state") or "").upper() != "CLOSED"
        or pr.get("mergedAt")
    ):
        return pr
    if not read:
        pr["replacement"] = {"status": "not_read"}
        return pr
    if not isinstance(pr.get("number"), int):
        pr["lookup_status"] = "replacement_lookup_failed"
        pr["error"] = "the closed PR record carries no number to read its head by"
        return pr
    answer, error = _replacement_read(repo, issue, pr["number"])
    opened_ts = utc_epoch.from_iso(pr.get("createdAt")) if pr.get("createdAt") else None
    judged = (
        _unanswered_replacement(error)
        if error is not None
        else judge_replacement(
            answer,
            repo=repo,
            pr_number=pr["number"],
            started_ts=started_ts,
            opened_ts=opened_ts,
        )
    )
    if judged["status"] == "unanswered":
        pr["lookup_status"] = "replacement_lookup_failed"
        pr["error"] = judged["error"]
    else:
        pr["replacement"] = judged
    return pr


def _local_pr_state(
    target: str,
    agent: str | None = None,
    pushes: dict | None = None,
    started_ts: int | None = None,
) -> dict | None:
    """Live: a LOCAL delegate's target is an ISSUE (owner/repo#N); its agent opened a PR on the
    deterministic branch orchestrator/issue-N (provision.py), or on a branch it named itself.
    Resolve that PR's state (most recent if several) so local-agent delegations close the loop the
    same way remote ones do.

    `pushes` is the run's push record (`feedback.run_pushes`). Its branches are asked FIRST, through
    `_pushed_branch_pr`, and are not asked again among the name-pattern candidates. Without a READ
    record that has branches, every lookup below is exactly what it was before the record existed.

    The run's own PR found CLOSED unmerged is read once more before it can be the FAIL: does a PR
    linked to issue N carry its head commit (`_attach_replacement`, after `started_ts`, the run's
    start)? A target that is itself a PR has no issue to read: its FAIL stands, marked `not_read`.
    """
    repo, num = provision.parse_target(target)
    if num is None:
        return {"lookup_status": "invalid_target", "target": target}
    direct_pr = _pr_view(repo, num)
    if direct_pr:
        return _attach_replacement(direct_pr, repo, num, started_ts, read=False)

    pushed = pushed_branches.recorded_branches(pushes)
    rejected: list[str] = []
    if pushed and pushes is not None:
        pr, rejected = _pushed_branch_pr(repo, pushed, int(pushes["window_start"]))
        if pr is not None:
            pr["target"] = target
            pr["candidateBranches"] = pushed
            if rejected:
                pr["pushedBranchRejected"] = rejected
            return _attach_replacement(pr, repo, num, started_ts)
    candidates = [
        branch for branch in _local_candidate_branches(num, agent) if branch not in pushed
    ]
    asked = pushed + candidates
    extra = {"pushedBranchRejected": rejected} if rejected else {}
    pr = _branch_pr_lookup(repo, candidates)
    if pr is not None:
        # FOUND: its state decides. Unanswered: it carries no state, so the run is skipped.
        pr["target"] = target
        pr["candidateBranches"] = asked
        pr.update(extra)
        return _attach_replacement(pr, repo, num, started_ts)
    terminal_issue = _closed_issue_without_branch_pr(repo, num, asked[0])
    if terminal_issue is not None:
        terminal_issue["candidateBranches"] = asked
        terminal_issue.update(extra)
        return terminal_issue
    return {
        "lookup_status": "no_pr_for_branch",
        "target": target,
        "branch": asked[0],
        "candidateBranches": asked,
        **extra,
    }


def _closing_pr_name(ref: object, repo: str) -> str:
    """`#N` for a closing PR in the issue's own repo, `owner/name#N` for one elsewhere. Reads both
    shapes a reference arrives in: `gh issue view`'s `repository {name owner {login}}` and the
    merge-time read's `repository {nameWithOwner}`."""
    if not isinstance(ref, dict):
        return str(ref)
    number = ref.get("number")
    where = ref.get("repository")
    where = where if isinstance(where, dict) else {}
    owner = where.get("owner")
    owner = owner if isinstance(owner, dict) else {}
    ref_repo = f"{owner.get('login')}/{where.get('name')}" if owner and where.get("name") else repo
    if isinstance(where.get("nameWithOwner"), str):
        ref_repo = where["nameWithOwner"]
    if number is None:
        return str(ref.get("url") or ref)
    return f"#{number}" if ref_repo == repo else f"{ref_repo}#{number}"


def _merged_by_close(ref: object, closed_ts: int) -> bool | None:
    """Pure: did this closing reference MERGE at or before the issue closed, within
    `CLOSING_PR_MERGE_SLACK_SECONDS`? Three answers, because a reference can be read three ways.
    True: it merged by the close, so it can have closed the issue. False: GitHub answered that it
    merged after the close, or that it has not merged at all (`mergedAt` null on an OPEN or CLOSED
    PR); either way it did not close this issue. None: its merge time cannot be read, which is an
    unknown and never "it did not close it"."""
    if not isinstance(ref, dict) or not isinstance(ref.get("number"), int) or "mergedAt" not in ref:
        return None
    merged_at = ref.get("mergedAt")
    if merged_at is None:
        return False if ref.get("state") in ("OPEN", "CLOSED") else None
    merged_ts = utc_epoch.from_iso(merged_at)
    if merged_ts is None:
        return None
    return merged_ts <= closed_ts + CLOSING_PR_MERGE_SLACK_SECONDS


def _closing_pr_merges(repo: str, num: int) -> dict:
    """Live: `CLOSING_PR_QUERY`, every page. `{"closedAt": iso, "refs": [node, ...]}`, or
    `{"error": why}` when GitHub did not answer in full: gh failed, a page does not parse, the pages
    disagree on the close time, or the last page says more exist."""
    owner, _, name = repo.partition("/")
    r = subprocess.run(
        [
            "gh",
            "api",
            "graphql",
            "--paginate",
            "--slurp",
            "-f",
            f"query={CLOSING_PR_QUERY}",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-F",
            f"number={num}",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if r.returncode != 0:
        return {"error": (r.stderr or r.stdout or "").strip()[:500]}
    try:
        pages = json.loads(r.stdout)
    except Exception:
        pages = None
    if not isinstance(pages, list) or not pages:
        return {"error": "the closing-PR read printed no pages"}
    closed_at: set = set()
    refs: list = []
    more: object = None
    for page in pages:
        data = page.get("data") if isinstance(page, dict) else None
        repository = data.get("repository") if isinstance(data, dict) else None
        issue = repository.get("issueOrPullRequest") if isinstance(repository, dict) else None
        conn = issue.get("closedByPullRequestsReferences") if isinstance(issue, dict) else None
        nodes = conn.get("nodes") if isinstance(conn, dict) else None
        info = conn.get("pageInfo") if isinstance(conn, dict) else None
        if not isinstance(issue, dict) or not isinstance(nodes, list) or not isinstance(info, dict):
            return {"error": "a closing-PR page has no reference list"}
        closed_at.add(issue.get("closedAt"))
        refs.extend(nodes)
        more = info.get("hasNextPage")
    if more is not False:
        return {"error": f"the closing-PR read stopped with hasNextPage={more!r}"}
    if len(closed_at) != 1:
        return {
            "error": f"the closing-PR pages disagree on the close time: {sorted(map(str, closed_at))}"
        }
    return {"closedAt": closed_at.pop(), "refs": refs}


def _judge_closing_prs(repo: str, num: int) -> dict:
    """Live: which of the issue's references closed it. `closing_prs` names the ones that merged by
    the close (`_merged_by_close`) and `late_closing_prs` the ones that did not, each with its merge
    time, so a FAIL or an unattributed note can say what it did not count. `{"error": why}` when the
    merge times could not all be read: one unread reference could be the one that closed it."""
    merges = _closing_pr_merges(repo, num)
    if "error" in merges:
        return merges
    closed_ts = utc_epoch.from_iso(merges["closedAt"])
    if closed_ts is None:
        # A CLOSED issue has a close time; none means it reopened after `gh issue view` read it,
        # and the next ingest reads it OPEN and waits.
        return {"error": f"the closing-PR read has no close time: {merges['closedAt']!r}"}
    counted: list[str] = []
    late: list[str] = []
    for ref in merges["refs"]:
        verdict = _merged_by_close(ref, closed_ts)
        if verdict is None:
            return {"error": f"a closing PR's merge time cannot be read: {ref!r}"[:500]}
        name = _closing_pr_name(ref, repo)
        if verdict:
            counted.append(name)
        else:
            merged = ref.get("mergedAt")
            late.append(f"{name} ({f'merged {merged}' if merged else 'not merged'})")
    return {
        "closedAt": merges["closedAt"],
        "closing_pr_count": len(counted),
        "closing_prs": counted,
        "late_closing_prs": late,
    }


def _closed_issue_without_branch_pr(repo: str, num: int, branch: str) -> dict | None:
    """When a delegate never opened a PR on any candidate branch but the issue is now closed, the
    run is terminal: no future PR state can arrive for those branches, so the outcome gap must not
    remain permanently actionable. The issue's closing PRs (`closing_prs`) decide the verdict in
    `state_to_outcome`, so they are part of the answer: the references that MERGED by the time the
    issue closed. The ones that merged later or not at all are `late_closing_prs`, named and not
    counted. GitHub lists a PR that links the issue after it closed too, and that PR cannot have
    closed it.

    Three answers: the closed-issue dict, None when GitHub answered and the issue is not closed, or
    an unanswered dict when gh could not say. That carries no state, so it is skipped. A CLOSED
    issue whose closing-PR list did not come back is unanswered too (`issue_lookup_failed`): whether
    someone delivered is the question the verdict turns on, and a missing list cannot say "nobody
    did". So is one whose references' merge times did not all come back
    (`closing_pr_lookup_failed`): an unread reference may be the one that closed it.
    """
    unanswered = {
        "lookup_status": "issue_lookup_failed",
        "target": f"{repo}#{num}",
        "branch": branch,
    }
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
        timeout=60,
    )
    if r.returncode != 0:
        return {**unanswered, "error": (r.stderr or r.stdout or "").strip()[:500]}
    try:
        issue = json.loads(r.stdout)
    except Exception:
        issue = None
    state = issue.get("state") if isinstance(issue, dict) else None
    # OPEN waits; MERGED is what `gh issue view` prints for a PR number (gh 2.94), not a closed
    # issue. Anything else is a state this verdict does not know, so it is not an answer either.
    if not isinstance(state, str) or state.upper() not in ISSUE_VIEW_STATES:
        return {**unanswered, "error": f"gh issue view printed no known issue state: {state!r}"}
    if state.upper() != "CLOSED":
        return None
    refs = issue.get("closedByPullRequestsReferences") if isinstance(issue, dict) else None
    if not isinstance(refs, list):
        return {**unanswered, "error": f"gh issue view printed no closing-PR list: {refs!r}"}
    answer = {
        "lookup_status": "closed_issue_no_branch_pr",
        "target": f"{repo}#{num}",
        "branch": branch,
        "state": "CLOSED",
        "number": issue.get("number"),
        "title": issue.get("title"),
        "url": issue.get("url"),
        "closedAt": issue.get("closedAt"),
        "closing_pr_count": 0,
        "closing_prs": [],
        "late_closing_prs": [],
    }
    if not refs:
        return answer  # no reference at all: nothing to time, and no second read
    judged = _judge_closing_prs(repo, num)
    if "error" in judged:
        return {**unanswered, "lookup_status": "closing_pr_lookup_failed", "error": judged["error"]}
    answer.update(judged)
    return answer


def state_to_outcome(pr: dict | None) -> dict | None:
    """Pure: map a gh PR state -> feedback.record_outcome kwargs. OPEN -> None (still pending, re-check
    later). MERGED -> success with durability='pending' (a later sweep confirms it actually held).
    CLOSED-unmerged -> abandoned failure. A lookup that could not answer maps to nothing.

    A closed issue with no PR on any candidate branch is terminal, and its closing PRs decide how:
    the references that merged by the time it closed (`closing_pr_count`; the rest are named in the
    notes and decide nothing). None: the run delivered nothing that landed, an abandoned FAIL. Some:
    the issue was delivered, and nothing says by this run, so the row records NO verdict and no
    merge state, only that the run is over (durability 'abandoned', the lifecycle end) and why it
    is not evidence (`feedback.UNATTRIBUTED_CLOSING_PR`, which every learner excludes). Reading it
    as FAIL trained the runs whose own PR closed the issue as failures; reading it as PASS would
    credit a run with a PR it cannot be shown to have produced; and leaving it pending would re-ask
    GitHub about an issue that can never change, with nothing to drain it.

    A remote delegation's own settled PR (`delegation`) is credited, PASS or FAIL, only when its
    `attribution` says the delegation can be shown to have produced it (`_delegation_outcome`).

    A local run's own PR closed unmerged carries the replacement read (`replacement`), which
    decides between the FAIL and the merge of a replacement that carries its head commit
    (`_replacement_outcome`). A closed PR without one is the FAIL, as it always was."""
    if not pr or pr.get("lookup_status") in UNANSWERED_LOOKUPS:
        return None
    st = (pr.get("state") or "").upper()
    if pr.get("delegation") and (st in ("MERGED", "CLOSED") or pr.get("mergedAt")):
        return _delegation_outcome(pr)
    # A PR the run opened on a branch it pushed is named in the notes as `PR #N merged`, the form
    # durability_sweep.EXPLICIT_MERGED_PR_RE reads, so the sweep resolves the exact PR for a run
    # whose target is the issue. Until 2026-10-04 that contract had no code writer.
    own = pr.get("credited_via") == PUSHED_BRANCH
    own_pr = f"local delegate PR #{pr.get('number')}"
    own_branch = f"on the branch this run pushed ({pr.get('headRefName') or pr.get('branch')})"
    rejected = "; ".join(pr.get("pushedBranchRejected") or [])
    rejected_note = f"; a PR on a branch this run pushed predates the run: {rejected}"
    if st == "MERGED" or pr.get("mergedAt"):
        return {
            "merged": True,
            "adjudicated_verdict": "PASS",
            "durability": "pending",
            "notes": (
                f"{own_pr} merged {own_branch}; durability pending sweep"
                if own
                else "remote keepalive PR merged; durability pending sweep"
            ),
        }
    if st == "CLOSED":
        if pr.get("lookup_status") in CLOSED_ISSUE_LOOKUPS:
            closing_pr_count = pr.get("closing_pr_count") or 0
            kind = (
                "remote delegate"
                if pr.get("lookup_status") == "closed_issue_no_remote_pr"
                else "local delegate"
            )
            # The references GitHub lists that did not merge by the close: named, never counted.
            late = pr.get("late_closing_prs") or []
            late_note = (
                f"; references that merged after the close at {pr.get('closedAt')} or never "
                f"merged, not counted: {', '.join(late)}"
                if late
                else ""
            )
            if closing_pr_count:
                closing = ", ".join(pr.get("closing_prs") or []) or f"{closing_pr_count} PR(s)"
                return {
                    "merged": None,
                    "adjudicated_verdict": None,
                    "durability": "abandoned",
                    "failure_class": feedback.UNATTRIBUTED_CLOSING_PR,
                    "notes": (
                        f"{kind} issue closed by a PR no candidate branch produced ({closing}); "
                        f"not attributed to this run; closing_pr_count={closing_pr_count}"
                        + late_note
                        + (rejected_note if rejected else "")
                    ),
                }
            no_closing = "no closing PR merged by the close" if late else "no closing PR references"
            if pr.get("delegation_without_own_pr"):
                # A remote delegation's agent runs only on the PR its label bootstraps, so with none
                # the labelled agent never ran: not its failure (owner decision 2026-10-04, amending
                # #411 for remote delegations; a local run did run, and keeps the FAIL below).
                branches = ", ".join(pr.get("candidateBranches") or []) or "its own branch"
                passed = pr.get("passed_over_pr")
                return {
                    "merged": None,
                    "adjudicated_verdict": None,
                    "durability": "abandoned",
                    "failure_class": feedback.UNATTRIBUTED_DELEGATION,
                    "notes": (
                        f"remote delegation's issue closed with no PR on {branches} and "
                        f"{no_closing}: the labelled agent never had a PR to run on"
                        + (f" ({passed} there settled before the label)" if passed else "")
                        + late_note
                        + "; not attributed to this run"
                    ),
                }
            notes = f"{kind} issue closed without matching branch PR; {no_closing}" + late_note
            notes += rejected_note if rejected else ""
        elif own:
            notes = f"{own_pr} closed unmerged {own_branch}"
        else:
            notes = "remote keepalive PR closed unmerged"
        replacement = pr.get("replacement")
        if isinstance(replacement, dict):
            # Only a local run's own closed PR carries the replacement read (`_attach_replacement`).
            return _replacement_outcome(pr, replacement, notes)
        return {
            "merged": False,
            "adjudicated_verdict": "FAIL",
            "durability": "abandoned",
            "notes": notes,
        }
    return None


def _replacement_outcome(pr: dict, replacement: dict, closed_notes: str) -> dict | None:
    """A local run's own PR closed unmerged, and the replacement read answered
    (`judge_replacement`). A credited replacement is the run's merge, through the same write path
    as a merge of its own PR, and the only form here that names `PR #N merged`, which the
    durability sweep resolves the merge from (`EXPLICIT_MERGED_PR_RE`, first match): it leads the
    notes (REPLACEMENT_CREDIT), so nothing in `closed_notes` can stand in front of it. An open one
    waits. The rest are the FAIL, or an outcome no learner scores; each names what was found."""
    status = replacement.get("status")
    head = str(replacement.get("head") or "")[:12]
    closed = f"closed PR #{pr.get('number')} ({pr.get('headRefName') or pr.get('branch')})"
    fail = {"merged": False, "adjudicated_verdict": "FAIL", "durability": "abandoned"}
    if status == "credited":
        link = replacement["pr"]
        return {
            "merged": True,
            "adjudicated_verdict": "PASS",
            "durability": "pending",
            "notes": (
                f"{REPLACEMENT_CREDIT}{link['number']} merged ({link.get('headRefName')}) "
                f"carrying the head {head} of its {closed}; durability pending sweep; "
                f"{closed_notes}"
            ),
        }
    if status == "unattributable":
        return {
            "merged": None,
            "adjudicated_verdict": None,
            "durability": "abandoned",
            "failure_class": feedback.UNATTRIBUTED_REPLACEMENT,
            "notes": (
                f"{closed_notes}; {REPLACEMENT_CHECK}: whether a PR linked to its issue carries "
                f"the head {head} of its {closed} cannot be told: {replacement.get('reason')}; "
                "not attributed to this run"
            ),
        }
    if status == "not_read":
        return {
            **fail,
            "notes": f"{closed_notes}; {REPLACEMENT_CHECK}: not read, the run's target is the "
            "pull request itself, so no issue links to it",
        }
    if status == "none":
        before = replacement.get("before_run") or []
        links = replacement.get("links") or []
        if replacement.get("gone"):
            found = "the run's issue does not resolve on GitHub, so no PR links it"
        else:
            found = (
                f"no PR linked to its issue carries the head {head} of its {closed}"
                + (f" after the run started (only {', '.join(before)})" if before else "")
                + (f"; links read: {', '.join(links)}" if links else "; no other PR links it")
            )
        return {**fail, "notes": f"{closed_notes}; {REPLACEMENT_CHECK}: {found}"}
    return None  # open: the run waits on that PR, as on its own open PR


def _delegation_outcome(pr: dict) -> dict:
    """A remote delegation's own PR, settled. Credit needs a positive attribution: anything else
    ends the run with no verdict and no merge state, under `feedback.UNATTRIBUTED_DELEGATION`, which
    no learner scores. Only the credited merge names `PR #N merged`, the form durability_sweep
    resolves an issue-target run's PR from (`_explicit_merged_pr_target`)."""
    merged = (pr.get("state") or "").upper() == "MERGED" or bool(pr.get("mergedAt"))
    head = f" ({pr['headRefName']})" if pr.get("headRefName") else ""
    marked = pr.get("attribution")
    attribution: dict = marked if isinstance(marked, dict) else {}
    reason = attribution.get("reason") or "it carries no attribution"
    if attribution.get("attributable") is not True:
        return {
            "merged": None,
            "adjudicated_verdict": None,
            "durability": "abandoned",
            "failure_class": feedback.UNATTRIBUTED_DELEGATION,
            "notes": (
                f"remote delegation's PR #{pr.get('number')}{head} "
                f"{'merged' if merged else 'closed unmerged'}, but {reason}; "
                "not attributed to this run"
            ),
        }
    if merged:
        return {
            "merged": True,
            "adjudicated_verdict": "PASS",
            "durability": "pending",
            "notes": (
                f"remote delegation's PR #{pr.get('number')} merged{head}; {reason}; "
                "durability pending sweep"
            ),
        }
    return {
        "merged": False,
        "adjudicated_verdict": "FAIL",
        "durability": "abandoned",
        "notes": f"remote delegation's PR #{pr.get('number')}{head} closed unmerged; {reason}",
    }


def _skip_reason(pr: dict | None) -> str:
    if not pr:
        return "state_unavailable"
    replacement = pr.get("replacement")
    if isinstance(replacement, dict) and replacement.get("status") == "open":
        return "open_replacement_pr"
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
        "credited_via",
        "pushedBranchRejected",
        "direct_target_pr",
        "title",
        "error",
        "direct_lookup_error",
        "passed_over_pr",
        "attribution",
        "replacement",
    ):
        if pr.get(key):
            detail[key] = pr[key]
    return detail


def _local_run_sql() -> str:
    """`is_local_delegate` in SQL over `runs r`: the same LEGACY_LOCAL_MODES, so the two cannot
    drift. Its parameters are LEGACY_LOCAL_MODES, in order."""
    legacy_marks = ",".join("?" for _ in LEGACY_LOCAL_MODES)
    return f"(r.mode='local' OR (r.mode IN ({legacy_marks}) AND instr(r.target,'#')>0))"


def _pending_runs(mode: str) -> list[dict]:
    with feedback._conn() as c:
        if mode != "local":
            rows = c.execute(
                "SELECT r.run_id, r.target, r.agent, r.pr_number, "
                "o.run_id IS NOT NULL, COALESCE(o.durability,''), r.ts, r.source "
                "FROM runs r LEFT JOIN outcomes o ON r.run_id=o.run_id "
                "WHERE (o.run_id IS NULL OR o.durability='pending') AND r.mode=?",
                (mode,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT r.run_id, r.target, r.agent, r.pr_number, "
                "o.run_id IS NOT NULL, COALESCE(o.durability,''), r.ts, r.source "
                "FROM runs r LEFT JOIN outcomes o ON r.run_id=o.run_id "
                "WHERE (o.run_id IS NULL OR o.durability='pending') AND " + _local_run_sql(),
                LEGACY_LOCAL_MODES,
            ).fetchall()
    return [
        {
            "run_id": rid,
            "target": target,
            "agent": agent,
            "pr_number": pr_number,
            "has_outcome": bool(has_outcome),
            "existing_durability": durability,
            "ts": ts,
            "source": source,
        }
        for rid, target, agent, pr_number, has_outcome, durability, ts, source in rows
    ]


def _pending_durability_detail(run: dict) -> dict:
    return {
        "run_id": run["run_id"],
        "target": run["target"],
        "durability": run.get("existing_durability") or "pending",
    }


def backfill_triage_disagreements(*, limit: int = 100, _state_fn=None) -> dict:
    """Resolve rejected triage edges observationally, never turn them into accepted credit.

    The historical population is measured now, rather than assuming the old
    issue's count still holds. Existing attribution guards decide whose PR it
    was; a merge stays pending until the normal durability sweep judges it.
    """
    with feedback._conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT e.edge_id,r.* FROM influence_edges e "
                "JOIN runs s ON s.run_id=e.source_run_id JOIN runs r ON r.run_id=e.target_run_id "
                "WHERE s.role_name='triage' AND e.influence_type='role' AND e.accepted=0 "
                "AND e.counterfactual=1 AND e.outcome_verdict IS NULL AND r.mode != 'role' "
                "ORDER BY e.created_ts LIMIT ?",
                (max(0, limit),),
            )
        ]
    result: dict = {"source": "backfill", "candidates": len(rows), "graded": [], "pending": []}
    for row in rows:
        try:
            if _state_fn:
                state = _state_fn(row["target"])
            elif is_local_delegate(row.get("mode"), row.get("target")):
                state = _local_pr_state(
                    row["target"], row.get("agent"), pushes=feedback.run_pushes(row["run_id"])
                )
            elif needs_delegation_guard(row.get("source")):
                state = _delegated_pr_state(row["target"], row.get("agent"), row.get("ts"))
            else:
                state = _pr_state(row["target"], row.get("agent"))
        except (OSError, subprocess.SubprocessError) as exc:
            result["pending"].append({"target": row["target"], "error": str(exc)})
            continue
        observed = state_to_outcome(state)
        if observed is None:
            result["pending"].append({"target": row["target"], "state": state})
            continue
        with feedback._conn() as conn:
            # Revalidate the edge after the network read; another ingest may have graded it.
            current = conn.execute(
                "SELECT accepted,outcome_verdict FROM influence_edges WHERE edge_id=?",
                (row["edge_id"],),
            ).fetchone()
            if not current or current[0] or current[1] is not None:
                continue
            existing = conn.execute(
                "SELECT durability FROM outcomes WHERE run_id=?", (row["run_id"],)
            ).fetchone()
            if existing:
                stored = conn.execute(
                    "SELECT adjudicated_verdict,verifier_verdict,merged,durability,failure_class "
                    "FROM outcomes WHERE run_id=?",
                    (row["run_id"],),
                ).fetchone()
                verdict = stored[0] or stored[1]
                merged, durability, failure_class = stored[2], stored[3], stored[4]
            else:
                observed["notes"] = "source=backfill; triage disagreement; " + observed.get(
                    "notes", ""
                )
                feedback._record_outcome_in_conn(conn, row["run_id"], **observed)
                verdict = observed.get("adjudicated_verdict") or observed.get("verifier_verdict")
                merged, durability, failure_class = (
                    observed.get("merged"),
                    observed.get("durability"),
                    observed.get("failure_class"),
                )
            if failure_class in feedback.LEARNING_EXCLUDED_FAILURE_CLASSES:
                verdict = "UNATTRIBUTED"
            conn.execute(
                "UPDATE influence_edges SET outcome_verdict=?,merged=?,durability=?,propagated_ts=? "
                "WHERE edge_id=? AND accepted=0 AND outcome_verdict IS NULL",
                (verdict, merged, durability, int(time.time()), row["edge_id"]),
            )
        result["graded"].append(
            {
                "target": row["target"],
                "verdict": verdict,
                "durability": durability,
                "accepted": False,
            }
        )
    return result


def ingest_outcomes(mode: str = "remote", dry_run: bool = False, _state_fn=None) -> dict:
    """For each delegated run lacking a resolved outcome, read its PR state and record the outcome.
    Remote runs may target a direct PR or a labeled opener issue whose keepalive PR branch is
    {agent}/issue-N; a remote DELEGATION is credited with that PR only when it can be shown to be
    its work (`_delegated_pr_state`). LOCAL delegate runs target an ISSUE whose agent opened a PR on
    branch orchestrator/issue-N. `_state_fn` overrides the live gh lookup (tests). Returns a
    summary; idempotent (record_outcome patches)."""
    pending = _pending_runs(mode)
    recorded, skipped = [], []
    pending_durability = []
    # Local runs only: a remote delegation runs on GitHub, so there is no worktree to have read and
    # the counts would describe nothing (`None`, not zeros). For local runs all four keys are always
    # present: `absent` and `unreadable` are the runs still resolved by name patterns alone.
    push_records = (
        {"credited": 0, "rejected": 0, "absent": 0, "unreadable": 0} if mode == "local" else None
    )
    # Local runs only, for the same reason: what the replacement read answered for each own PR
    # found closed unmerged. `waiting` is the blocking number of the one new wait (an open PR that
    # carries the run's head), and every one of them is drainable: the next ingest asks again.
    replacements = dict.fromkeys(REPLACEMENT_COUNTS.values(), 0) if mode == "local" else None
    for run in pending:
        if run.get("has_outcome") and run.get("existing_durability") == "pending":
            pending_durability.append(_pending_durability_detail(run))
            continue
        pushes = feedback.run_pushes(run["run_id"]) if push_records is not None else None
        if push_records is not None:
            if pushes is None:
                push_records["absent"] += 1
            elif pushes["status"] == "unreadable":
                push_records["unreadable"] += 1
        if _state_fn:
            pr = _state_fn(run["target"])
        elif mode == "local":
            pr = _local_pr_state(
                run["target"], run.get("agent"), pushes=pushes, started_ts=run.get("ts")
            )
        elif needs_delegation_guard(run.get("source")):
            pr = _delegated_pr_state(run["target"], run.get("agent"), run.get("ts"))
        else:
            pr = _pr_state(run["target"], run.get("agent"))
        if push_records is not None and pr and pr.get("pushedBranchRejected"):
            push_records["rejected"] += 1
        replacement = pr.get("replacement") if isinstance(pr, dict) else None
        if replacements is not None and isinstance(replacement, dict):
            replacements[REPLACEMENT_COUNTS[replacement["status"]]] += 1
        oc = state_to_outcome(pr)
        if oc is None:
            skipped.append(_skip_detail(run, pr))
            continue
        if not dry_run:
            feedback.record_outcome(run["run_id"], **oc)
        credited_via = pr.get("credited_via") if pr else None
        if push_records is not None and credited_via == PUSHED_BRANCH:
            push_records["credited"] += 1
        recorded.append(
            {
                "run_id": run["run_id"],
                "merged": oc["merged"],
                "durability": oc["durability"],
                "failure_class": oc.get("failure_class"),
                "credited_via": credited_via,
                "replacement": replacement.get("status") if replacement else None,
            }
        )
    return {
        "mode": mode,
        "pending": len(pending),
        "recorded": len(recorded),
        "skipped": len(skipped),
        # Skipped because GitHub could not answer, not because the work is still open: each one is
        # retried next ingest. Always present, so a clean run reads `0` rather than a missing key.
        "unanswered": sum(1 for row in skipped if row["reason"] in UNANSWERED_LOOKUPS),
        # Recorded, and terminal, but scored by no learner: the issue closed through a PR no
        # candidate branch produced, or a delegation's own PR settled without its agent's work on
        # it. Always present for the same reason as `unanswered`.
        "unattributed": sum(1 for row in recorded if row["failure_class"] in UNATTRIBUTED_CLASSES),
        "push_records": push_records,
        "replacements": replacements,
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
    skipped_details = [detail for row in results for detail in row.get("skipped_details", [])]
    pending_durability_details = [
        detail for row in results for detail in row.get("pending_durability_details", [])
    ]
    return {
        "mode": mode,
        "dry_run": dry_run,
        "results": results,
        "pending": sum(row.get("pending", 0) for row in results),
        "recorded": sum(row.get("recorded", 0) for row in results),
        "skipped": sum(row.get("skipped", 0) for row in results),
        "unanswered": sum(row.get("unanswered", 0) for row in results),
        "unattributed": sum(row.get("unattributed", 0) for row in results),
        # Only the local path reads push records, so its counts are the whole answer; with no local
        # path in this run there is nothing to report, which is None and never a row of zeros.
        "push_records": next(
            (row["push_records"] for row in results if row.get("push_records") is not None), None
        ),
        "replacements": next(
            (row["replacements"] for row in results if row.get("replacements") is not None), None
        ),
        "pending_durability": sum(row.get("pending_durability", 0) for row in results),
        "skipped_details": skipped_details,
        "pending_durability_details": pending_durability_details,
    }


# THE RE-JUDGE of the rows written before the replacement read existed. Ingest never revisits a
# decided row, so the FAIL it wrote for a closed own PR stands unless something asks again, and this
# does, once per row: a local run's unclassified FAIL whose notes say its PR closed unmerged and
# carry no REPLACEMENT_CHECK. Every verdict ingest now writes on such a PR carries that marker, so
# only the old rows qualify. It re-reads the row with ingest's own resolver and applies the answer
# only to the PR the FAIL was written about: closed unmerged no later than the FAIL's own time. A
# credited replacement writes the merge through `feedback.record_outcome`, as ingest does; a FAIL
# that stands, or an outcome no learner scores, gains its REPLACEMENT_CHECK note and leaves the
# selection. A wait (an open carrier, a read GitHub did not answer) lasts at most
# REPLACEMENT_RECHECK_HORIZON_DAYS from the row's first selection, then the FAIL stands with its
# reason, so the selection drains to a printed zero. Before its first write each row's whole outcome
# row and the completion events `record_outcome` keeps for it are snapshotted, and
# `--undo-replacement-recheck` restores both. "0" in the switch stops the selection, and so does
# the owner's newest answer to the owner question whose text carries REPLACEMENT_RECHECK_TOKEN, when
# it says "decline" (the owner-question protocol: it runs on its default until then).
REPLACEMENT_RECHECK_SWITCH = "ORCH_REPLACEMENT_RECHECK"
REPLACEMENT_RECHECK_UNDO = "outcomes-replacement-recheck-undo.jsonl"
REPLACEMENT_RECHECK_HORIZON_DAYS = 7
REPLACEMENT_RECHECK_TOKEN = "[replacement-recheck]"
# The one word that stops the re-judge, as the first word of the owner's answer ("decline",
# "Declined: ..."). One word, said in the question: free text cannot be read reliably, and "no
# objection" must not read as "no".
DECLINE_RE = re.compile(r"\s*declin", re.IGNORECASE)
RECHECK_DECIDED = ("credited", "unattributable", "none", "not_read")
RECORD_OUTCOME_PRODUCER = "feedback.record_outcome"


def _recheck_undo_path() -> Path:
    state_dir = os.environ.get("ORCH_STATE_DIR") or Path.home() / ".codex" / "orchestrator"
    return Path(state_dir) / REPLACEMENT_RECHECK_UNDO


def _replacement_recheck_runs() -> list[dict]:
    """The rows the re-judge may change, oldest first. Unclassified only: a classified FAIL is
    already excluded from learning, and a patch here could not clear its class."""
    with feedback._conn() as c:
        rows = c.execute(
            "SELECT r.run_id, r.target, r.agent, r.ts, o.notes, o.durability_checked_ts "
            "FROM runs r JOIN outcomes o ON r.run_id=o.run_id WHERE "
            + _local_run_sql()
            + " AND o.adjudicated_verdict='FAIL' AND COALESCE(o.merged,0)=0 "
            "AND o.durability='abandoned' AND COALESCE(o.failure_class,'')='' "
            "AND instr(COALESCE(o.notes,''),'closed unmerged')>0 "
            "AND instr(COALESCE(o.notes,''),?)=0 ORDER BY r.ts, r.run_id",
            (*LEGACY_LOCAL_MODES, REPLACEMENT_CHECK),
        ).fetchall()
    keys = ("run_id", "target", "agent", "ts", "notes", "durability_checked_ts")
    return [dict(zip(keys, row)) for row in rows]


def _columns(c, table: str) -> list[str]:
    return [str(row[1]) for row in c.execute(f"PRAGMA table_info({table})")]


def _recheck_snapshot(c, run_id: str) -> dict:
    """The row's whole outcome and the completion events `record_outcome` keeps for it."""
    columns = _columns(c, "outcomes")
    row = c.execute(
        f"SELECT {', '.join(columns)} FROM outcomes WHERE run_id=?", (run_id,)
    ).fetchone()
    event_columns = _columns(c, "completion_events")
    events = c.execute(
        f"SELECT {', '.join(event_columns)} FROM completion_events "
        "WHERE run_id=? AND producer=? ORDER BY event_id",
        (run_id, RECORD_OUTCOME_PRODUCER),
    ).fetchall()
    return {
        "outcome": dict(zip(columns, row)) if row else None,
        "events": [dict(zip(event_columns, event)) for event in events],
    }


def _recheck_snapshots() -> dict[str, dict]:
    """run_id -> its FIRST snapshot entry, the state before the re-judge first touched it."""
    try:
        lines = _recheck_undo_path().read_text().splitlines()
    except OSError:
        return {}
    first: dict[str, dict] = {}
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("run_id") and entry["run_id"] not in first:
            first[entry["run_id"]] = entry
    return first


def _recheck_declined() -> dict | None:
    """The owner's decline: the NEWEST answer to an owner question carrying the token, when its
    first word is "decline". A later answer lifts an earlier decline; none, or expiry, runs."""
    with feedback._conn() as c:
        row = c.execute(
            "SELECT question_id, answer FROM owner_questions "
            "WHERE instr(question, ?) > 0 AND status='answered' "
            "ORDER BY COALESCE(answered_ts, ts) DESC, question_id DESC LIMIT 1",
            (REPLACEMENT_RECHECK_TOKEN,),
        ).fetchone()
    if row and DECLINE_RE.match(str(row[1] or "")):
        return {"question_id": row[0], "answer": row[1]}
    return None


def _recorded_pr(pr: object, run: dict) -> bool:
    """Is `pr` the PR this row's FAIL was written about? The resolver asks a branch for its NEWEST
    PR, so a later run's PR on the same branch can answer instead. The recorded one had closed
    unmerged by the time the FAIL was written (`durability_checked_ts`)."""
    if not isinstance(pr, dict) or pr.get("lookup_status") != "found":
        return False
    closed_ts = utc_epoch.from_iso(pr.get("closedAt")) if pr.get("closedAt") else None
    written = run.get("durability_checked_ts")
    return bool(
        str(pr.get("state") or "").upper() == "CLOSED"
        and not pr.get("mergedAt")
        and closed_ts is not None
        and isinstance(written, int)
        and closed_ts <= written
    )


def _recheck_wait_reason(pr: object) -> str:
    """Why a selected row cannot be decided on this run."""
    if not isinstance(pr, dict):
        return "GitHub did not answer"
    if pr.get("lookup_status") in UNANSWERED_LOOKUPS:
        return f"GitHub did not answer: {pr.get('error') or pr.get('lookup_status')}"
    replacement = pr.get("replacement")
    if isinstance(replacement, dict) and replacement.get("status") == "open":
        return f"a PR carrying its head is still open: {', '.join(replacement.get('prs') or [])}"
    return "its closed PR has no replacement read"


def recheck_replacements(*, dry_run: bool = False, now: int | None = None, _resolve=None) -> dict:
    """Re-judge each selected row once (see REPLACEMENT_RECHECK_SWITCH's comment). `_resolve`
    overrides the live resolver (tests). Every count is always present: a drained run prints
    `selected 0`, a stopped one still counts what it would select, and `waiting`, the blocking
    number, prints beside `drains_by`, the date its last row closes, since every waiting row
    drains either on an answer or at its horizon."""
    now = int(now if now is not None else time.time())
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    runs = _replacement_recheck_runs()
    summary: dict = {
        "selected": len(runs),
        "credited": 0,
        "unattributable": 0,
        "kept_fail": 0,
        "not_the_recorded_pr": 0,
        "closed_at_horizon": 0,
        "waiting": 0,
        "drains_by": None,
        "snapshotted": 0,
        "stopped": None,
        "dry_run": dry_run,
        "undo": "python3 src/outcomes.py --undo-replacement-recheck",
        "details": [],
    }
    declined = _recheck_declined()
    if os.environ.get(REPLACEMENT_RECHECK_SWITCH, "1") == "0":
        summary["stopped"] = f"{REPLACEMENT_RECHECK_SWITCH}=0"
    elif declined:
        summary["stopped"] = f"declined: owner question {declined['question_id']}"
    if summary["stopped"] or not runs:
        return summary
    if not dry_run:
        with feedback._conn() as connection:
            question_exists = connection.execute(
                "SELECT 1 FROM owner_questions WHERE instr(question, ?) > 0 LIMIT 1",
                (REPLACEMENT_RECHECK_TOKEN,),
            ).fetchone()
        if not question_exists:
            feedback.record_owner_question(
                f"{REPLACEMENT_RECHECK_TOKEN} Re-judge closed-PR FAIL rows; "
                "answer decline to stop this recheck.",
                "run it",
            )
    snapshots = _recheck_snapshots()
    fresh = [run for run in runs if run["run_id"] not in snapshots]
    if fresh and not dry_run:
        path = _recheck_undo_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with feedback._conn() as c, path.open("a") as log:
            for run in fresh:
                entry = {"run_id": run["run_id"], "selected_ts": now}
                entry.update(_recheck_snapshot(c, run["run_id"]))
                log.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
        summary["snapshotted"] = len(fresh)
        snapshots = _recheck_snapshots()

    def resolve(run: dict) -> dict | None:
        pushes = feedback.run_pushes(run["run_id"])
        return _local_pr_state(
            run["target"], run.get("agent"), pushes=pushes, started_ts=run.get("ts")
        )

    for run in runs:
        found = (_resolve or resolve)(run)
        pr = found if isinstance(found, dict) else {}
        stored = str(run.get("notes") or "")
        answered = bool(pr) and pr.get("lookup_status") not in UNANSWERED_LOOKUPS
        replacement = pr.get("replacement") if answered else None
        status = replacement.get("status") if isinstance(replacement, dict) else None
        write: dict
        if answered and not _recorded_pr(pr, run):
            number = pr.get("number")
            shown = f"#{number}" if isinstance(number, int) else pr.get("lookup_status")
            action, write = "not_the_recorded_pr", {
                "notes": f"{stored}; {REPLACEMENT_CHECK}: its branch now shows {shown} "
                f"({pr.get('state')}), not the PR this FAIL was written about; FAIL kept ({day})"
            }
        elif isinstance(replacement, dict) and status in RECHECK_DECIDED:
            judged = _replacement_outcome(pr, replacement, stored)
            assert judged is not None, replacement
            notes = f"{judged['notes']} (re-judged {day}; was FAIL)"
            if status == "credited":
                action, write = "credited", {**judged, "notes": notes}
            elif status == "unattributable":
                # A patch cannot clear the stored merge state or verdict; the class is what every
                # learner reads, the same shape `mark_transient_infra` leaves on a FAIL.
                action = "unattributable"
                write = {"failure_class": judged["failure_class"], "notes": notes}
            else:
                action, write = "kept_fail", {"notes": notes}
        else:
            first = snapshots.get(run["run_id"], {}).get("selected_ts")
            started = first if isinstance(first, int) else now
            closes = started + REPLACEMENT_RECHECK_HORIZON_DAYS * 86400
            why = _recheck_wait_reason(found)
            if now < closes:
                summary["waiting"] += 1
                summary["drains_by"] = max(filter(None, (summary["drains_by"], _iso(closes))))
                summary["details"].append(
                    {
                        "run_id": run["run_id"],
                        "target": run["target"],
                        "action": "waiting",
                        "reason": why,
                    }
                )
                continue
            action, write = "closed_at_horizon", {
                "notes": f"{stored}; {REPLACEMENT_CHECK}: undecided for "
                f"{REPLACEMENT_RECHECK_HORIZON_DAYS} days ({why}); FAIL kept ({day})"
            }
        summary[action] += 1
        summary["details"].append(
            {
                "run_id": run["run_id"],
                "target": run["target"],
                "action": action,
                "notes": write["notes"],
            }
        )
        if not dry_run:
            feedback.record_outcome(run["run_id"], **write)
    return summary


def undo_replacement_recheck() -> dict:
    """Restore every row the re-judge touched to its first snapshot: the whole outcome row and the
    completion events `record_outcome` keeps for it, so the record of what happened reads as it did
    before. Then re-propagate over its accepted influence edges, which carry a target's CURRENT
    state, so they follow it back. A row whose run has gone is skipped and named."""
    path = _recheck_undo_path()
    first = _recheck_snapshots()
    if not first:
        return {"restored": 0, "reason": f"no undo log at {path}"}
    restored, skipped = 0, []
    with feedback._conn() as c:
        outcome_columns = set(_columns(c, "outcomes"))
        event_columns = set(_columns(c, "completion_events"))
        for run_id, entry in first.items():
            outcome = entry.get("outcome")
            events = entry.get("events")
            if (
                not isinstance(outcome, dict)
                or not isinstance(events, list)
                or not set(outcome) <= outcome_columns
                or not all(isinstance(e, dict) and set(e) <= event_columns for e in events)
            ):
                skipped.append(run_id)
                continue
            fields = [field for field in outcome if field != "run_id"]
            c.execute(
                f"UPDATE outcomes SET {', '.join(f'{field}=?' for field in fields)} "
                "WHERE run_id=?",
                (*(outcome[field] for field in fields), run_id),
            )
            c.execute(
                "DELETE FROM completion_events WHERE run_id=? AND producer=?",
                (run_id, RECORD_OUTCOME_PRODUCER),
            )
            for event in events:
                c.execute(
                    f"INSERT INTO completion_events ({', '.join(event)}) "
                    f"VALUES ({', '.join('?' for _ in event)})",
                    tuple(event.values()),
                )
            feedback._propagate_outcome_lineage_in_conn(c, run_id)
            restored += 1
    return {
        "restored": restored,
        "skipped": skipped,
        "log": str(path),
        "keep_it": (
            f"export {REPLACEMENT_RECHECK_SWITCH}=0, or answer the owner question carrying "
            f"{REPLACEMENT_RECHECK_TOKEN} with 'decline'; otherwise the next local ingest "
            "re-judges these rows"
        ),
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
    assert state_to_outcome({"state": "OPEN"}) is None and state_to_outcome(None) is None
    assert _local_candidate_branches(9, "codex") == [
        "orchestrator/issue-9",
        "codex/issue-9",
        "cursor/issue-9",
        "vibe/issue-9",
        "gemini/issue-9",
        "claude/issue-9",
    ]
    # end-to-end: two remote runs, one merged one open -> merged recorded, open skipped
    feedback.record_run("remote:o/r#1:cursor", "o/r#1", "implement", "cursor", mode="remote")
    feedback.record_run("remote:o/r#2:codex", "o/r#2", "implement", "codex", mode="remote")
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
    # ...but a closing PR means someone delivered: terminal, no verdict, a class no learner scores.
    closed_by_pr = state_to_outcome(
        {
            "lookup_status": "closed_issue_no_remote_pr",
            "state": "CLOSED",
            "closing_pr_count": 1,
            "closing_prs": ["#3067"],
        }
    )
    assert closed_by_pr.get("failure_class") == feedback.UNATTRIBUTED_CLOSING_PR, (
        "a closing PR no candidate branch produced was recorded as a verdict on this run",
        closed_by_pr,
    )
    assert (closed_by_pr["merged"], closed_by_pr["adjudicated_verdict"]) == (None, None)
    assert closed_by_pr["durability"] != "pending" and "#3067" in closed_by_pr["notes"]
    assert feedback.UNATTRIBUTED_CLOSING_PR in feedback.LEARNING_EXCLUDED_FAILURE_CLASSES
    with feedback._conn() as c:
        row = c.execute(
            "SELECT merged, durability FROM outcomes WHERE run_id='remote:o/r#1:cursor'"
        ).fetchone()
    assert row and row[0] == 1 and row[1] == "pending", row  # merged recorded, durability pending
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
    assert any(row["run_id"] == "legacy-local-composer" for row in legacy["details"]), legacy
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
    assert any(
        row["run_id"] == "legacy-local-cheap" for row in legacy_cheap["details"]
    ), legacy_cheap
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
    feedback.record_run("remote:o/r#4:vibe", "o/r#4", "implement", "vibe", mode="remote")
    feedback.record_run("o__r_5-cursor-456", "o/r#5", "implement", "cursor", mode="local")
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
    both_skips = [row for result in both["results"] for row in result.get("skipped_details", [])]
    assert any(row["reason"] == "open_pr" for row in both_skips), both
    assert any(row["reason"] == "state_unavailable" for row in both_skips), both
    assert both["unanswered"] == 0, both
    # A lookup that cannot answer is not "no PR": every branch lookup rate-limited while the issue
    # reads CLOSED must write NO outcome row (skipped, counted unanswered), and the next ingest that
    # gets answers records the real verdict. Own store, so only this run is pending; stubbed gh.
    feedback.DB_PATH = Path(tmp) / "unanswered.db"
    feedback.record_run("remote:o/r#8:codex", "o/r#8", "implement", "codex", mode="remote")
    real_run = subprocess.run

    def _gh(pr_list: tuple):
        def fake_run(argv, **_kw):
            if argv[1:3] == ["pr", "list"]:
                return subprocess.CompletedProcess(argv, *pr_list)
            if argv[1:3] == ["issue", "view"]:
                closed = {"state": "CLOSED", "closedByPullRequestsReferences": []}
                return subprocess.CompletedProcess(argv, 0, json.dumps(closed), "")
            return subprocess.CompletedProcess(argv, 1, "", "not a pull request")

        return fake_run

    try:
        subprocess.run = _gh((1, "", "API rate limit exceeded"))
        rate_limited = ingest_outcomes(mode="remote")
        subprocess.run = _gh((0, "[]", ""))
        answered = ingest_outcomes(mode="remote")
    finally:
        subprocess.run = real_run
    assert (rate_limited["recorded"], rate_limited["unanswered"]) == (0, 1), (
        "unanswered branch lookups reached the closed-issue verdict",
        rate_limited,
    )
    assert rate_limited["skipped_details"][0]["reason"] == "lookup_failed", rate_limited
    assert (answered["recorded"], answered["unanswered"]) == (1, 0), answered
    assert answered["details"][0]["durability"] == "abandoned", answered
    # A reference that merged AFTER the issue closed did not close it (Workflows#2819, closed by
    # hand; its references merged 23 to 48 days later): named, not counted, so the closed issue
    # keeps the abandoned FAIL. A reference whose merge time cannot be read decides nothing: the
    # run is unanswered, never "no closing PR".
    closed_2819 = {
        "state": "CLOSED",
        "closedAt": "2026-08-15T13:18:25Z",
        "closedByPullRequestsReferences": [{"number": 3402}],
    }

    def _gh_late(closing: tuple):
        def fake_run(argv, **_kw):
            if argv[1:3] == ["issue", "view"]:
                return subprocess.CompletedProcess(argv, 0, json.dumps(closed_2819), "")
            if argv[1:3] == ["api", "graphql"]:
                return subprocess.CompletedProcess(argv, *closing)
            raise AssertionError(f"unexpected gh call: {argv}")

        return fake_run

    try:
        subprocess.run = _gh_late(
            _closing_read("2026-08-15T13:18:25Z", (3402, "2026-09-07T14:58:30Z"))
        )
        late = _closed_issue_without_branch_pr("o/r", 2819, "orchestrator/issue-2819")
        subprocess.run = _gh_late(_closing_read("2026-08-15T13:18:25Z", (3402, "not a time")))
        unread = _closed_issue_without_branch_pr("o/r", 2819, "orchestrator/issue-2819")
    finally:
        subprocess.run = real_run
    late_outcome = state_to_outcome(late)
    assert late_outcome and late_outcome["adjudicated_verdict"] == "FAIL", (
        "a reference that merged after the issue closed was counted as the PR that closed it",
        late_outcome,
    )
    assert "#3402 (merged 2026-09-07T14:58:30Z)" in late_outcome["notes"], late_outcome
    assert unread and unread["lookup_status"] == "closing_pr_lookup_failed", unread
    assert state_to_outcome(unread) is None, "an unread merge time decided the verdict"
    _selftest_delegation_attribution()
    # A branch the run pushed (its push record) is asked FIRST, and the PR the run opened there is
    # its delivery: credited, and named `PR #N merged` for the durability sweep. A PR on that branch
    # created before the run started is not the run's. Own store, stubbed gh.
    feedback.DB_PATH = Path(tmp) / "pushed.db"
    feedback.record_run("o__r_12-codex-1", "o/r#12", "implement", "codex", mode="local")
    feedback.record_run_pushes(
        "o__r_12-codex-1",
        status="read",
        window_start=1000,
        window_end=2000,
        branches=[{"branch": "fix/12-own-name", "sha": "a" * 40, "pushed_ts": 1500}],
    )
    pr_created = {"at": "1970-01-01T00:25:00Z"}  # 1500: inside the run's window

    def _gh_pushed(argv, **_kw):
        if argv[1:3] == ["pr", "list"] and argv[argv.index("--head") + 1] == "fix/12-own-name":
            found = {
                "number": 13,
                "state": "MERGED",
                "mergedAt": "1970-01-01T00:30:00Z",
                "headRefName": "fix/12-own-name",
                "createdAt": pr_created["at"],
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps([found]), "")
        if argv[1:3] == ["pr", "list"]:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        if argv[1:3] == ["issue", "view"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"state": "OPEN"}), "")
        return subprocess.CompletedProcess(argv, 1, "", "not a pull request")

    try:
        subprocess.run = _gh_pushed
        own = ingest_outcomes(mode="local", dry_run=True)
        pr_created["at"] = "1970-01-01T00:16:00Z"  # 960: before the run started
        older = ingest_outcomes(mode="local", dry_run=True)
    finally:
        subprocess.run = real_run
    assert own["push_records"] == {"credited": 1, "rejected": 0, "absent": 0, "unreadable": 0}
    assert own["details"][0]["credited_via"] == PUSHED_BRANCH, own
    assert (
        "PR #13 merged on the branch this run pushed"
        in state_to_outcome(
            {"state": "MERGED", "number": 13, "headRefName": "b", "credited_via": PUSHED_BRANCH}
        )["notes"]
    )
    assert (older["recorded"], older["push_records"]["rejected"]) == (0, 1), (
        "a PR on a pushed branch that predates the run was credited to it",
        older,
    )
    _selftest_replacement()
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)
    print(
        "outcomes.py selftest: OK (state->outcome mapping, ingest records merged/abandoned, "
        "open skipped, merged stays pending for durability sweep, unanswered lookups skipped "
        "and retried rather than abandoned, a closing PR no candidate branch produced recorded "
        "as unattributed rather than failed, a reference that merged after the close not "
        "counted and an unread merge time unanswered, a delegation credited only with its own "
        "PR and its "
        "agent's completed runner rounds, a PR the run opened on a branch it pushed credited "
        "and one that predates the run rejected, a closed PR's replacement carrying its head "
        "credited and a different implementation not)"
    )


def _closing_read(closed: str | None, *refs: tuple) -> tuple:
    """What `_closing_pr_merges`'s `gh api graphql --paginate --slurp` prints, as a stub answer: one
    page holding the issue's close time and each `(number, mergedAt)` reference in `o/r`."""
    nodes = [
        {
            "number": number,
            "url": f"https://github.com/o/r/pull/{number}",
            "state": "MERGED" if merged else "OPEN",
            "mergedAt": merged,
            "repository": {"nameWithOwner": "o/r"},
        }
        for number, merged in refs
    ]
    conn = {"pageInfo": {"hasNextPage": False, "endCursor": "MQ"}, "nodes": nodes}
    issue = {"closedAt": closed, "closedByPullRequestsReferences": conn}
    return (0, json.dumps([{"data": {"repository": {"issueOrPullRequest": issue}}}]), "")


def _selftest_replacement() -> None:
    """GitHub's answers for Orchestrator#213/#214 and #199/#200, read 2026-10-05: the same-commit
    replacement is the run's merge, the different implementation is not, and neither is nothing.
    A closed PR opened before the run started is not shown to be the run's, so it earns nothing."""
    repo, start, opened = "stranske/Orchestrator", 1788481981, 1788482953
    head = "282949241f3c33569c1854e2f9f91f2be387207f"
    same = _replacement_link(219, "MERGED", "2026-09-04T06:26:24Z", [head, "9" * 40], repo=repo)
    other = _replacement_link(208, "MERGED", "2026-09-04T06:26:24Z", ["f" * 40], repo=repo)
    closed = {"number": 214, "state": "CLOSED", "headRefName": "orchestrator/issue-213"}

    def judged(*links: dict, opened_ts: int | None = opened) -> dict:
        answer = json.loads(_replacement_answer(214, head, *links)[1])
        return judge_replacement(
            answer, repo=repo, pr_number=214, started_ts=start, opened_ts=opened_ts
        )

    for links, verdict in (((same,), "PASS"), ((other,), "FAIL"), ((), "FAIL")):
        outcome = state_to_outcome({**closed, "replacement": judged(*links)})
        assert outcome and outcome["adjudicated_verdict"] == verdict, (links, outcome)
    credited = state_to_outcome({**closed, "replacement": judged(same)})
    assert credited and credited["notes"].startswith(f"{REPLACEMENT_CREDIT}219 merged"), credited
    assert judged(same, opened_ts=start - 1)["status"] == "unattributable"
    unread = judge_replacement(
        {"data": {}}, repo=repo, pr_number=214, started_ts=start, opened_ts=opened
    )
    assert unread["status"] == "unanswered", "an unread answer must never reach a verdict"


def _replacement_link(
    number: int,
    state: str,
    merged_at: str | None,
    oids: list[str],
    *,
    total: int | None = None,
    repo: str = "o/r",
    branch: str | None = None,
) -> dict:
    """One node of `REPLACEMENT_QUERY`'s link list, as GitHub prints it."""
    return {
        "number": number,
        "state": state,
        "mergedAt": merged_at,
        "headRefName": branch or f"codex/issue-{number}",
        "repository": {"nameWithOwner": repo},
        "commits": {
            "totalCount": len(oids) if total is None else total,
            "nodes": [{"commit": {"oid": oid}} for oid in oids],
        },
    }


def _replacement_answer(pr_number: int, head: str, *links: dict, more: bool = False) -> tuple:
    """What `_replacement_read`'s `gh api graphql` prints, as a stub answer: the closed PR's head
    and the issue's links (`_replacement_link`)."""
    refs = {"pageInfo": {"hasNextPage": more}, "nodes": list(links)}
    repository = {
        "pullRequest": {"number": pr_number, "headRefOid": head},
        "issueOrPullRequest": {"__typename": "Issue", "closedByPullRequestsReferences": refs},
    }
    return (0, json.dumps({"data": {"repository": repository}}), "")


def _runner_comment(kind: str, provider: str, pr: int, payload: dict, login: str) -> dict:
    """A PR comment carrying one runner marker, encoded exactly as runner_lib's `_build_marker`."""
    encoded = base64.b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    body = f"Runner dispatch state.\n\n<!-- {kind}:{provider}:{pr}:v1 base64:{encoded} -->"
    return {"user": {"login": login}, "author_association": "CONTRIBUTOR", "body": body}


def _selftest_delegation_attribution() -> None:
    label = 1_790_000_000
    after, before = _iso(label + 600), _iso(label - 600)

    def reservation(provider: str, pr: int, rid: str, started: str) -> dict:
        record = {"provider": provider, "pr_number": pr, "reservation_id": rid}
        record.update(status="pending", started_at=started)
        return _runner_comment("runner-reservation", provider, pr, record, "github-actions[bot]")

    def receipt(provider: str, pr: int, rid: str, started: str, **final) -> dict:
        record = {"provider": provider, "pr_number": pr, "reservation_id": rid}
        record.update(started_at=started, **final)
        payload = {"schema": RUNNER_RECEIPT_SCHEMA, "reservation_id": rid, "record": record}
        return _runner_comment("runner-completion", provider, pr, payload, "github-actions[bot]")

    comments = [
        reservation("gemini", 5, "r1", after),
        receipt("gemini", 5, "r1", after, status="completed", productive=True),
        reservation("gemini", 5, "r2", after),
        receipt("gemini", 5, "r2", after, status="completed", productive=False),
        reservation("gemini", 5, "r3", after),
        receipt("gemini", 5, "r3", after, status="error"),
        reservation("gemini", 5, "r4", before),
        reservation("codex", 5, "r5", after),
        {**reservation("gemini", 5, "r6", after), "user": {"login": "mallory"}},
    ]
    counts = runner_rounds(comments, provider="gemini", pr_number=5, since_ts=label)
    assert counts == {
        "credited": 1,
        "unproductive": 1,
        "errored": 1,
        "pending": 0,
        "before_label": 1,
        "undated": 0,
    }, counts
    assert runner_rounds(comments, provider="cursor", pr_number=5, since_ts=label)["credited"] == 0

    def gh(answers: dict):
        """Answers keyed `pr view`, `issue view`, `comments`, `closing` (the merge-time read) and
        `list:<head branch>`."""

        def fake_run(argv, **_kw):
            if argv[1:3] == ["pr", "list"]:
                key = f"list:{argv[argv.index('--head') + 1]}"
            elif argv[1] == "api":
                key = "closing" if argv[2] == "graphql" else "comments"
            else:
                key = " ".join(argv[1:3])
            calls.append(key)
            if key not in answers:
                raise AssertionError(f"unexpected gh call: {argv}")
            return subprocess.CompletedProcess(argv, *answers[key])

        return fake_run

    def resolve(target: str, agent: str, answers: dict) -> dict | None:
        real_run = subprocess.run
        subprocess.run = gh(answers)
        try:
            return state_to_outcome(_delegated_pr_state(target, agent, label))
        finally:
            subprocess.run = real_run

    not_a_pr = (1, "", "Could not resolve to a PullRequest")
    closed_by_other = {"state": "CLOSED", "closedByPullRequestsReferences": [{"number": 2627}]}
    # Workflows#2620: no PR of its own; the local lane's merged PR is never asked about or credited.
    calls: list = []
    outcome = resolve(
        "o/r#2620",
        "gemini",
        {
            "pr view": not_a_pr,
            "list:gemini/issue-2620": (0, "[]", ""),
            "issue view": (0, json.dumps(closed_by_other), ""),
            "closing": _closing_read(before, (2627, _iso(label - 601))),
        },
    )
    assert outcome and outcome.get("failure_class") == feedback.UNATTRIBUTED_CLOSING_PR, outcome
    assert not any("orchestrator/issue" in str(call) for call in calls), calls
    # Trend#5913: a labelled PR that merged with only codex rounds on it is not gemini's PASS.
    direct = {
        "number": 5913,
        "state": "MERGED",
        "mergedAt": _iso(label + 3000),
        "closedAt": _iso(label + 3000),
        "headRefName": "codex/issue-5858",
    }
    codex_round = [receipt("codex", 5913, "c1", after, status="completed", productive=True)]
    outcome = resolve(
        "o/r#5913",
        "gemini",
        {
            "pr view": (0, json.dumps(direct), ""),
            "comments": (0, json.dumps([codex_round]), ""),
        },
    )
    assert outcome and outcome.get("failure_class") == feedback.UNATTRIBUTED_DELEGATION, outcome
    assert (outcome["merged"], outcome["adjudicated_verdict"]) == (None, None), outcome
    # The legitimate case: its own branch, merged after the label, with a credited round.
    own = {**direct, "number": 70, "headRefName": "gemini/issue-7"}
    gemini_round = [
        reservation("gemini", 70, "g1", after),
        receipt("gemini", 70, "g1", after, status="completed", productive=True),
    ]
    outcome = resolve(
        "o/r#7",
        "gemini",
        {
            "pr view": not_a_pr,
            "list:gemini/issue-7": (0, json.dumps([own]), ""),
            "comments": (0, json.dumps([gemini_round]), ""),
        },
    )
    assert outcome and outcome["adjudicated_verdict"] == "PASS", outcome
    assert "PR #70 merged" in outcome["notes"], outcome


def main(argv):
    parser = argparse.ArgumentParser(description="Ingest delegated PR outcomes into feedback.py.")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--mode",
        choices=["remote", "local", "both"],
        default="remote",
        help="remote resolves direct keepalive PR targets; local resolves orchestrator/issue-N PR branches",
    )
    parser.add_argument(
        "--undo-replacement-recheck",
        action="store_true",
        help="restore every row the replacement re-judge changed to the state it snapshotted first",
    )
    args = parser.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    if args.undo_replacement_recheck:
        print(json.dumps(undo_replacement_recheck(), indent=2))
        return 0
    summary = ingest_modes(args.mode, dry_run=args.dry_run)
    # The local path re-judges the rows written before the replacement read existed: once each,
    # then it selects nothing (`recheck_replacements`).
    summary["replacement_recheck"] = (
        recheck_replacements(dry_run=args.dry_run) if args.mode in ("local", "both") else None
    )
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
