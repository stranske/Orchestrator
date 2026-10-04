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
Either verdict needs every candidate branch to have ANSWERED "no PR here": a lookup that could not
answer leaves the run pending, retried at the next ingest, and counted in the summary's `unanswered`.

A REMOTE DELEGATION (an `orchestrator_remote` run: the tick applied `agent:<X>` to an issue or PR)
is credited with a PR only when the PR can be shown to be that delegation's work, and three exact
conditions decide it (`_delegated_pr_state`). The run's own PR is on the delegated agent's own
keepalive branch, `{agent}/issue-N`, or is the labelled PR itself: another agent's or another lane's
branch is never this run's. A PR merged or closed before the label was applied cannot be its work.
And a PASS or a FAIL needs at least one COMPLETED round of the delegated agent's keepalive runner
on that PR since the label, not measured unproductive, read from the runner's own trusted markers.
A settled PR without that evidence records no verdict, `feedback.UNATTRIBUTED_DELEGATION`, a class
no learner scores. Until 2026-10-04 the resolver walked every agent's branch and
`orchestrator/issue-N` and credited the first PR found, and a labelled PR's merge went to whatever
agent the label named.
`--selftest` runs fully offline (mocked PR states + temp store).
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import subprocess
import sys
import time

import feedback
import provision
import utc_epoch

# A GitHub lookup here answers one of three ways: it FOUND the thing, it answered that there is
# NOTHING there, or it could not answer (gh failed, or printed something that does not parse). Only
# the second is evidence of absence. Reading the third as the second recorded a closed issue whose
# branch lookups had all failed as `abandoned`: an unknown written into the outcome labels the router
# learns from. So an unanswered lookup ends the resolution, the run is skipped with no outcome row,
# and the next ingest asks again. One set, read by the resolvers and by the summary's count.
UNANSWERED_LOOKUPS = frozenset(
    {"lookup_failed", "parse_failed", "issue_lookup_failed", "runner_rounds_lookup_failed"}
)
ISSUE_VIEW_STATES = frozenset({"OPEN", "CLOSED", "MERGED"})
# A closed target issue with every candidate branch answered "no PR". Which verdict it gets is decided
# by the issue's closing PRs, in `state_to_outcome`.
CLOSED_ISSUE_LOOKUPS = frozenset({"closed_issue_no_branch_pr", "closed_issue_no_remote_pr"})
# The verdict classes that mean "terminal, but not this run's work", counted as `unattributed`.
UNATTRIBUTED_CLASSES = frozenset(
    {feedback.UNATTRIBUTED_CLOSING_PR, feedback.UNATTRIBUTED_DELEGATION}
)
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


def runner_rounds(comments: list, *, provider: str, pr_number: int, since_ts: int) -> dict:
    """Pure: `provider`'s keepalive runner rounds on PR `pr_number`, read from the trusted runner
    markers in `comments` (oldest first, as the REST API lists them).

    A round is one dispatch: a reservation joined to its completion receipt by reservation id, a
    receipt whose reservation is not in view, or the legacy record. Only rounds that started at or
    after `since_ts` can be the delegation's, and one is CREDITED when it completed and was not
    measured unproductive: `productive` False is runner_lib's own "produced nothing" verdict, and a
    missing `productive` is unmeasured, which a completed legacy round always is. Error, pending and
    undated rounds are counted beside the credited ones and never credited, so a PR whose agent was
    dispatched and died on a usage limit is not that agent's work."""
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
    counts = dict.fromkeys(
        ("credited", "unproductive", "errored", "pending", "before_label", "undated"), 0
    )
    for record in rounds:
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
    its own the run gets the closed-issue verdicts, which the issue's closing PRs decide."""
    repo, num = provision.parse_target(target)
    if num is None:
        return {"lookup_status": "invalid_target", "target": target}
    r = subprocess.run(
        ["gh", "pr", "view", str(num), "-R", repo, "--json", PR_VIEW_FIELDS],
        capture_output=True,
        text=True,
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
    pr = _branch_pr_lookup(repo, candidates)
    if pr is not None:
        # FOUND: its state decides. Unanswered: it carries no state, so the run is skipped.
        pr["target"] = target
        pr["candidateBranches"] = candidates
        return pr
    terminal_issue = _closed_issue_without_branch_pr(repo, num, candidates[0])
    if terminal_issue is not None:
        terminal_issue["candidateBranches"] = candidates
        return terminal_issue
    return {
        "lookup_status": "no_pr_for_branch",
        "target": target,
        "branch": candidates[0],
        "candidateBranches": candidates,
    }


def _closing_pr_name(ref: object, repo: str) -> str:
    """`#N` for a closing PR in the issue's own repo, `owner/name#N` for one elsewhere."""
    if not isinstance(ref, dict):
        return str(ref)
    number = ref.get("number")
    where = ref.get("repository")
    where = where if isinstance(where, dict) else {}
    owner = where.get("owner")
    owner = owner if isinstance(owner, dict) else {}
    ref_repo = f"{owner.get('login')}/{where.get('name')}" if owner and where.get("name") else repo
    if number is None:
        return str(ref.get("url") or ref)
    return f"#{number}" if ref_repo == repo else f"{ref_repo}#{number}"


def _closed_issue_without_branch_pr(repo: str, num: int, branch: str) -> dict | None:
    """When a delegate never opened a PR on any candidate branch but the issue is now closed, the
    run is terminal: no future PR state can arrive for those branches, so the outcome gap must not
    remain permanently actionable. The issue's closing PRs (`closing_prs`) decide the verdict in
    `state_to_outcome`, so they are part of the answer.

    Three answers: the closed-issue dict, None when GitHub answered and the issue is not closed, or
    an `issue_lookup_failed` dict when gh could not say. The last carries no state, so it is skipped.
    A CLOSED issue whose closing-PR list did not come back is the third answer too: whether someone
    delivered is the question the verdict turns on, and a missing list cannot say "nobody did".
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
    return {
        "lookup_status": "closed_issue_no_branch_pr",
        "target": f"{repo}#{num}",
        "branch": branch,
        "state": "CLOSED",
        "number": issue.get("number"),
        "title": issue.get("title"),
        "url": issue.get("url"),
        "closedAt": issue.get("closedAt"),
        "closing_pr_count": len(refs),
        "closing_prs": [_closing_pr_name(ref, repo) for ref in refs],
    }


def state_to_outcome(pr: dict | None) -> dict | None:
    """Pure: map a gh PR state -> feedback.record_outcome kwargs. OPEN -> None (still pending, re-check
    later). MERGED -> success with durability='pending' (a later sweep confirms it actually held).
    CLOSED-unmerged -> abandoned failure. A lookup that could not answer maps to nothing.

    A closed issue with no PR on any candidate branch is terminal, and its closing PRs decide how.
    None: the run delivered nothing that landed, an abandoned FAIL. Some: the issue was delivered,
    and nothing says by this run, so the row records NO verdict and no merge state, only that the
    run is over (durability 'abandoned', the lifecycle end) and why it is not evidence
    (`feedback.UNATTRIBUTED_CLOSING_PR`, which every learner excludes). Reading it as FAIL trained
    the runs whose own PR closed the issue as failures; reading it as PASS would credit a run with a
    PR it cannot be shown to have produced; and leaving it pending would re-ask GitHub about an issue
    that can never change, with nothing to drain it.

    A remote delegation's own settled PR (`delegation`) is credited, PASS or FAIL, only when its
    `attribution` says the delegation can be shown to have produced it (`_delegation_outcome`)."""
    if not pr or pr.get("lookup_status") in UNANSWERED_LOOKUPS:
        return None
    st = (pr.get("state") or "").upper()
    if pr.get("delegation") and (st in ("MERGED", "CLOSED") or pr.get("mergedAt")):
        return _delegation_outcome(pr)
    if st == "MERGED" or pr.get("mergedAt"):
        return {
            "merged": True,
            "adjudicated_verdict": "PASS",
            "durability": "pending",
            "notes": "remote keepalive PR merged; durability pending sweep",
        }
    if st == "CLOSED":
        if pr.get("lookup_status") in CLOSED_ISSUE_LOOKUPS:
            closing_pr_count = pr.get("closing_pr_count") or 0
            kind = (
                "remote delegate"
                if pr.get("lookup_status") == "closed_issue_no_remote_pr"
                else "local delegate"
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
                    ),
                }
            notes = f"{kind} issue closed without matching branch PR; no closing PR references"
        else:
            notes = "remote keepalive PR closed unmerged"
        return {
            "merged": False,
            "adjudicated_verdict": "FAIL",
            "durability": "abandoned",
            "notes": notes,
        }
    return None


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
        "passed_over_pr",
        "attribution",
    ):
        if pr.get(key):
            detail[key] = pr[key]
    return detail


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
            # Older local delegates recorded their agent mode ("composer"/"full"/"cheap") in
            # runs.mode before stable local mode existed, and some cheap rows predate source
            # stamping. They still target an issue and resolve through the same
            # orchestrator/issue-N PR branch.
            rows = c.execute(
                "SELECT r.run_id, r.target, r.agent, r.pr_number, "
                "o.run_id IS NOT NULL, COALESCE(o.durability,''), r.ts, r.source "
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
    for run in pending:
        if run.get("has_outcome") and run.get("existing_durability") == "pending":
            pending_durability.append(_pending_durability_detail(run))
            continue
        if _state_fn:
            pr = _state_fn(run["target"])
        elif mode == "local":
            pr = _local_pr_state(run["target"], run.get("agent"))
        elif run.get("source") == DELEGATION_SOURCE:
            pr = _delegated_pr_state(run["target"], run.get("agent"), run.get("ts"))
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
                "failure_class": oc.get("failure_class"),
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
    _selftest_delegation_attribution()
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)
    print(
        "outcomes.py selftest: OK (state->outcome mapping, ingest records merged/abandoned, "
        "open skipped, merged stays pending for durability sweep, unanswered lookups skipped "
        "and retried rather than abandoned, a closing PR no candidate branch produced recorded "
        "as unattributed rather than failed, a delegation credited only with its own PR and its "
        "agent's completed runner rounds)"
    )


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
        """Answers keyed `pr view`, `issue view`, `comments` and `list:<head branch>`."""

        def fake_run(argv, **_kw):
            if argv[1:3] == ["pr", "list"]:
                key = f"list:{argv[argv.index('--head') + 1]}"
            else:
                key = "comments" if argv[1] == "api" else " ".join(argv[1:3])
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
    args = parser.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    print(json.dumps(ingest_modes(args.mode, dry_run=args.dry_run), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
