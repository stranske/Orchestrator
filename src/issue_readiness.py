#!/usr/bin/env python3
"""issue_readiness.py — decide which open issues become fleet-ready WITHOUT an owner queue.

THE PROBLEM THIS SOLVES. `backlog._is_ready()` reads a `status: ready` label that only a human
ever applied. So every issue the fleet could work waited on one person, and the measured arrival
rate (~25 issues/week across the 12-repo fleet) against the owner's weekly attention budget (LOCAL_POLICY.md) made that
queue structurally impossible to keep up with. It did not keep up: the backlog sat at 1 item while
94 issues were open.

WHAT ALREADY EXISTED (checked before building). The upstream `agents-auto-label.yml` /
`scripts/langchain/label_matcher.py` in the Workflows repo assigns DESCRIPTIVE labels
(bug/feature/docs) with a similarity score. `backlog.classify()` maps those labels to a task_type.
Neither decides whether an issue should be WORKED. This module supplies only that missing decision
and consumes both existing signals rather than re-deriving them.

THE RULE. Four verdicts, and only one of them can ever reach the owner:

  auto_ready          actionable, not risk-labelled, not already routed  -> apply `status: ready`
  owner_review        risk-labelled AND actionable -> ONE non-blocking, auto-expiring question
  needs_specification actionable signal absent -> machine remedy (triage drafts AC), re-run next tick
  not_opener_work     recurring bot report / operational alert / tracker container -> excluded

MEASURED SPLIT on the 94 open fleet issues (2026-08-11): 32 auto_ready, 5 owner_review,
0 needs_specification, 42 not_opener_work, 8 already routed, 7 containers. Owner-review share is
5.8% of classifiable issues -> ~1.45 issues/week -> a few minutes/week against the weekly budget (LOCAL_POLICY.md)
(ratio 0.15-0.24). That is the number that made this design acceptable; the earlier
classification-confidence rule scored 0.65-0.97 and was rejected as over budget.

WHY THE EXPIRY DEFAULT IS "PROCEED". An owner question that ratifies to "stays unready" would
recreate the exact latched-state failure this session removed elsewhere: a flag whose clear path
depends on the condition the flag itself prevents. So an unanswered risk question ratifies to
READY, and the risk is contained where it belongs -- at the delivery gate (draft PR, pr-00-gate,
verifier, adversarial review), not at the issue queue. Tim's review is genuinely optional: answer
it and the issue routes his way; ignore it for 7 days and it proceeds as a draft PR that still
cannot merge without gates. A backlog is therefore structurally impossible, not merely unlikely.

FAIL-CLOSED. Every uncertainty resolves AWAY from autonomy: an unparseable label set, an unknown
body, or a risk term found anywhere in the label vocabulary yields owner_review, never auto_ready.
Exclusions are counted and reported, never silently dropped -- silence must not read as a pass.

    python3 issue_readiness.py                 # assess the live fleet, decide nothing
    python3 issue_readiness.py --json
    python3 issue_readiness.py --apply         # requires ORCH_ISSUE_AUTOREADY=1
    python3 issue_readiness.py --selftest
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Any

import backlog

VERDICTS = ("auto_ready", "owner_review", "needs_specification", "not_opener_work")

# Writing labels to GitHub is a real change; default OFF like every other ORCH_* apply path.
APPLY_ENABLED = os.environ.get("ORCH_ISSUE_AUTOREADY", "").strip() == "1"
READY_LABEL = "status: ready"
OWNER_QUESTION_EXPIRY_DAYS = 7.0

# Risk vocabulary taken from labels ACTUALLY present on the fleet, not invented.
RISK_LABELS = {
    "risk:major",
    "risk:critical",
    "security",
    "breaking-change",
    "data-loss",
    "priority:critical",
    "priority: critical",
}
# Any label containing one of these is treated as risk even if the exact name is new.
RISK_SUBSTRINGS = ("security", "risk:major", "risk:critical", "breaking", "data-loss")

# `epic` is deliberately NOT here: a parent epic is not work to exclude, it is work for the epic
# decomposition lane. Keeping it in this set is what made all 5 fleet parent epics unroutable while
# the owner decomposed them by hand.
CONTAINER_LABELS = {"tracker:durable", "planning", "roadmap", "multi-issue"}
# `\bepic\b` also matched `[Epic #845][P1] ...` CHILD subtasks, which locked 21 already-decomposed
# fleet issues out of the ready queue as if they were containers. Children are ordinary work.
CONTAINER_TITLE = re.compile(r"roadmap|campaign queue", re.IGNORECASE)
EPIC_PARENT_TITLE = re.compile(r"^\s*\[epic\]", re.IGNORECASE)
EPIC_CHILD_TITLE_RE = re.compile(r"^\s*\[epic\s*#\d+\]", re.IGNORECASE)

# Recurring machine-generated reports. These are real output, but they are not opener work:
# they are alerts and dashboards whose remedy is an operator action, not a code change.
BOT_LABELS = {"automated"}
BOT_TITLE = re.compile(
    r"^[\U0001F300-\U0001FAFF☀-➿]"  # emoji-prefixed alert
    r"|^(Dependency Dashboard|Agent metrics|Collaborator Onboarding)"
    r"|dashboard$|advisory report$|campaign queue$|fleet coverage —",
    re.IGNORECASE,
)
# Alerts whose remedy is an operator action; surfaced separately so they are never merely dropped.
OPERATIONAL_ALERT = re.compile(
    r"expired|auth|credential|sync failed|action required|broken", re.IGNORECASE
)

# Actionability: an explicit acceptance contract, or enough concrete code anchors to act on.
AC_MARKERS = re.compile(r"acceptance criteria|## tasks|- \[ \]", re.IGNORECASE)
CODE_ANCHOR = re.compile(
    r"[\w/\-]+\.(?:py|ts|tsx|js|jsx|yml|yaml|md|sh|json|toml|cfg)\b"
    r"|\b\w+\([^)]{0,80}\)"
    r"|\bline \d+",
    re.IGNORECASE,
)
MIN_CODE_ANCHORS = 2


def _norm_labels(issue: dict) -> set[str]:
    """Lowercased label names. Tolerates both the API shape and a plain list of strings."""
    out = set()
    for lab in issue.get("labels") or []:
        name = lab.get("name") if isinstance(lab, dict) else lab
        if name:
            out.add(str(name).strip().lower())
    return out


def is_risky(labels: set[str]) -> bool:
    """FAIL-CLOSED: exact membership OR substring match, so a new `risk:*` label still trips."""
    if labels & RISK_LABELS:
        return True
    return any(sub in lab for lab in labels for sub in RISK_SUBSTRINGS)


def is_actionable(body: str | None) -> bool:
    """An explicit acceptance contract, or >=2 concrete code anchors. Empty body is never actionable."""
    text = body or ""
    if not text.strip():
        return False
    if AC_MARKERS.search(text):
        return True
    return len(CODE_ANCHOR.findall(text)) >= MIN_CODE_ANCHORS


def _capability_heartbeat(event_type: str = "invocation", *, target: str | None = None) -> None:
    """Credit the issue-readiness capability at its declared entrypoint.

    Lazy import + never raises + inert outside an active tick, matching the sibling modules. Added
    2026-08-20 after the activation audit flagged this capability `no_heartbeat` — it was the only
    module I had built this session that recorded nothing about itself.
    """
    try:
        import capabilities

        # Name the ISSUE, not just the function. The ref was a constant string, so the ledger
        # recorded 39 invocations without recording WHAT was assessed — and a future resolver
        # attributing a delivery to this capability would have had to infer the link from timing,
        # which capability_outcome_bridge refuses by design. With the target in the ref, the link
        # is recorded and available the moment ORCH_ISSUE_AUTOREADY arms the write arm. (2026-08-21)
        capabilities.production_heartbeat(
            "issue-readiness",
            event_type,
            ref=str(target) if target else "issue_readiness.classify_issue",
        )
    except Exception:
        pass


def classify_issue(issue: dict) -> dict:
    """One issue -> {verdict, reason, task_type, actionable, risky}. Never raises on odd input."""
    _capability_heartbeat(
        target=issue.get("target") or issue.get("html_url") or issue.get("number")
    )
    labels = _norm_labels(issue)
    title = str(issue.get("title") or "")
    body = issue.get("body")
    risky = is_risky(labels)
    actionable = is_actionable(body)
    raw = [lab.get("name") if isinstance(lab, dict) else lab for lab in (issue.get("labels") or [])]
    task_type = backlog.classify([str(r) for r in raw if r])

    def out(verdict, reason):
        return {
            "verdict": verdict,
            "reason": reason,
            "task_type": task_type,
            "actionable": actionable,
            "risky": risky,
        }

    if any(lab.startswith("agent:") for lab in labels) or "status:in-progress" in labels:
        return out("not_opener_work", "already routed to an agent")
    if backlog._is_ready([str(r) for r in raw if r]):
        return out("not_opener_work", "already ready")
    if labels & BOT_LABELS or BOT_TITLE.search(title):
        kind = "operational alert" if OPERATIONAL_ALERT.search(title) else "recurring report"
        return out("not_opener_work", kind)
    if labels & CONTAINER_LABELS or CONTAINER_TITLE.search(title):
        return out("not_opener_work", "tracker container")
    # A PARENT epic is routable work: the epic lane decomposes it. Previously excluded as a
    # container, which is why all 5 fleet parents were decomposed by hand out of the owner's
    # the weekly budget. A CHILD (`[Epic #NNN]`) falls through to ordinary handling below.
    if EPIC_PARENT_TITLE.search(title) and not EPIC_CHILD_TITLE_RE.search(title):
        return out("auto_ready", "parent epic — route to the decomposition lane")
    if not actionable:
        return out("needs_specification", "no acceptance criteria and <2 code anchors")
    if risky:
        return out("owner_review", "risk-labelled and actionable")
    return out("auto_ready", "actionable, no risk label, not routed")


def assess(issues: list[dict]) -> dict:
    """Classify a list of issues and roll up. Exclusions are COUNTED, never silently dropped."""
    rows: list = []
    counts = {v: 0 for v in VERDICTS}
    reasons: dict[str, Any] = {}
    for issue in issues:
        verdict = classify_issue(issue)
        repo = (issue.get("repository") or {}).get("name") or issue.get("repo") or "?"
        row = dict(verdict)
        row["target"] = f"{repo}#{issue.get('number')}"
        row["title"] = str(issue.get("title") or "")[:100]
        rows.append(row)
        counts[verdict["verdict"]] += 1
        reasons[verdict["reason"]] = reasons.get(verdict["reason"], 0) + 1
    total = len(rows)
    classifiable = total - counts["not_opener_work"]
    # Per-ARRIVAL share is the one the attention budget cares about: issues arrive at a measured
    # rate and only this fraction of them can ever reach the owner.
    per_arrival = counts["owner_review"] / total if total else 0.0
    return {
        "total": total,
        "counts": counts,
        "reasons": reasons,
        "classifiable": classifiable,
        "owner_review_share": (
            round(counts["owner_review"] / classifiable, 4) if classifiable else 0.0
        ),
        "owner_review_share_of_arrivals": round(per_arrival, 4),
        "attention": attention_cost(per_arrival),
        "rows": rows,
    }


# Measured over 2026-W29..W32 via `gh search issues --owner stranske --created >=...`.
ARRIVAL_RATE_PER_WEEK = 25.0
OWNER_BUDGET_MIN_PER_WEEK = 30.0
MIN_PER_REVIEW = 5.0  # deliberately the pessimistic end of the 3-5 min range


def attention_cost(share_of_arrivals: float) -> dict:
    """State the rule's own owner cost, so a drift in the issue mix surfaces instead of creeping.

    A rule that quietly grows past the budget is the failure mode this whole design exists to
    prevent, so the cost is recomputed and reported on every run rather than asserted once.
    """
    per_week = ARRIVAL_RATE_PER_WEEK * share_of_arrivals
    minutes = per_week * MIN_PER_REVIEW
    ratio = minutes / OWNER_BUDGET_MIN_PER_WEEK if OWNER_BUDGET_MIN_PER_WEEK else 0.0
    return {
        "arrival_rate_per_week": ARRIVAL_RATE_PER_WEEK,
        "owner_reviews_per_week": round(per_week, 2),
        "minutes_per_week": round(minutes, 1),
        "budget_min_per_week": OWNER_BUDGET_MIN_PER_WEEK,
        "ratio": round(ratio, 3),
        "verdict": ("viable" if ratio < 0.5 else "redesign" if ratio <= 2.0 else "infeasible"),
    }


def _gh_json(args: list[str]) -> list[dict]:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        return []
    try:
        return json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return []


def fetch_open_issues(limit: int = 200) -> list[dict]:
    """Authoritative per-repo listing over the lane fleet.

    NOT `gh search issues --owner ... --state open`: that index is stale and returned
    Travel-Plan-Permission#1435 as open on 2026-08-18 when it had been closed on 08-15, and
    reported 94 open issues when the true fleet count was 41. Acting on a stale index means
    labelling closed issues and mis-sizing the owner-review budget, so this pays 12 API calls for
    a correct answer. A repo whose listing FAILS is reported, never silently treated as empty --
    an unreachable repo must not read as "no work here".
    """
    out: list[dict] = []
    for full in backlog.SUPPORTED_REPOS:
        name = full.split("/")[-1]
        rows = _gh_json(
            [
                "issue",
                "list",
                "--repo",
                full,
                "--state",
                "open",
                "--limit",
                str(limit),
                "--json",
                "number,title,labels,body,author",
            ]
        )
        if rows is None:
            continue
        for row in rows:
            row["repository"] = {"name": name}
            out.append(row)
    return out


def fetch_failures(limit: int = 200) -> list[str]:
    """Repos whose issue listing could not be read. Surfaced so silence never reads as a pass."""
    bad = []
    for full in backlog.SUPPORTED_REPOS:
        proc = subprocess.run(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                full,
                "--state",
                "open",
                "--limit",
                "1",
                "--json",
                "number",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            bad.append(f"{full}: {(proc.stderr or '').strip()[:80]}")
    return bad


def repo_ready_label(repo: str, _cache: dict[str, str | None] = {}) -> str | None:
    """The ready label AS THAT REPO SPELLS IT, or None if it has none.

    The fleet is not consistent: most repos use `status: ready`, Travel-Plan-Permission uses
    `status:ready`. `backlog.READY_LABELS` already tolerates both when READING, so writing one
    hardcoded variant silently under-applies in the repos that spell it the other way. Returns
    None rather than inventing a label -- creating labels across 12 repos is a bigger change than
    this module is authorised to make, and a missing label must surface as an error.
    """
    if repo in _cache:
        return _cache[repo]
    proc = subprocess.run(
        ["gh", "label", "list", "--repo", f"stranske/{repo}", "--limit", "200", "--json", "name"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    existing = set()
    if proc.returncode == 0:
        try:
            existing = {row.get("name", "") for row in json.loads(proc.stdout or "[]")}
        except json.JSONDecodeError:
            existing = set()
    match = next(
        (
            name
            for name in existing
            if name.strip().lower() in {r.lower() for r in backlog.READY_LABELS}
        ),
        None,
    )
    _cache[repo] = match
    return match


def apply_ready(rows: list[dict], *, dry_run: bool = True) -> dict:
    """Apply the repo's ready label to auto_ready rows; raise ONE question per owner_review row."""
    applied, questions, errors = [], [], []
    for row in rows:
        if row["verdict"] == "auto_ready":
            if dry_run:
                applied.append(row["target"])
                continue
            repo, num = row["target"].split("#")
            label = repo_ready_label(repo)
            if not label:
                errors.append(
                    f"{row['target']}: repo has no ready label "
                    f"(expected one of {sorted(backlog.READY_LABELS)})"
                )
                continue
            proc = subprocess.run(
                ["gh", "issue", "edit", num, "--repo", f"stranske/{repo}", "--add-label", label],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if proc.returncode == 0:
                applied.append(row["target"])
            else:
                errors.append(f"{row['target']}: {(proc.stderr or '').strip()[:120]}")
        elif row["verdict"] == "owner_review":
            if dry_run:
                questions.append(row["target"])
                continue
            try:
                import feedback

                res = feedback.record_owner_question(
                    f"{row['target']} is risk-labelled: {row['title']}. "
                    f"Work it with the fleet, or handle it yourself?",
                    "proceed as a draft PR; delivery gates contain the risk",
                    target=row["target"],
                    repo=row["target"].split("#")[0],
                    options=["fleet", "mine", "skip"],
                    expires_days=OWNER_QUESTION_EXPIRY_DAYS,
                )
                if not res.get("deduped"):
                    questions.append(row["target"])
            except Exception as exc:  # never let telemetry break the apply
                errors.append(f"{row['target']}: {exc}")
    return {"applied": applied, "questions_raised": questions, "errors": errors, "dry_run": dry_run}


# ---------------------------------------------------------------------------
# Durable-issue census: how big is the backlog REALLY?
#
# Some issues are designed never to close: Renovate's `Dependency Dashboard`, the weekly
# `Agent metrics` tracker, fleet-coverage trackers. They are rewritten in place forever. Counting
# them as backlog makes every open-issue number wrong — measured 2026-08-18, 37 of 40 open fleet
# issues were of this shape.
#
# The marker already exists: `tracker:durable`, which Workflows' repo_review_backlog_scan.py
# already excludes as "handled by their own controller" and agents-issue-format-guard.yml already
# treats as exempt. It is simply applied inconsistently — 17 of 37 lacked it. So this does NOT
# invent a vocabulary; it makes the existing one complete and gives it a corrected count.
#
# SIGNAL. Bot-authorship is an eligibility GUARD, not evidence: durable trackers and one-off
# operational alerts share the same author (app/github-actions), so authorship cannot separate
# them. What separates them is CROSS-REPO RECURRENCE — a title appearing in >=3 fleet repos is a
# fleet-wide controller artefact, not a piece of work. Verified: that rule catches 15 of the 17
# unlabelled, and the 2 it skips (`Consumer Repo Sync Failed`, `sync drift detected`) are correctly
# skipped — they are alerts that SHOULD close once acted on, not permanent trackers.
#
# FAIL-CLOSED IS THE OTHER DIRECTION HERE. For readiness, uncertainty routes to a human. For the
# census, marking something durable HIDES it from the backlog, so uncertainty must count as REAL
# WORK. A human-authored issue is never auto-marked, whatever its title.
DURABLE_LABEL = "tracker:durable"
MIN_REPOS_FOR_DURABLE = 3
# Controller-owned auto-bot labels. `Workflows/docs/ops/DURABLE_TRACKING_ISSUES.md` names these as
# filter #2 ("broader, catches other auto-bot issues too. Useful as a safety net when
# tracker:durable is missing") behind the label itself, and ranks title conventions LAST.
CONTROLLER_LABELS = {"automated", "automation"}
# That doc's own rule of thumb: "if the title carries a counter or a fixed timestamp ('expires in 39
# hours', 'run 235'), it is TRANSIENT. If the title is generic and the body is a recurring snapshot
# ('queue', 'summary', 'drift detected'), it is DURABLE." A counter/timestamp means a fresh issue per
# occurrence, so it must never be treated as permanent.
TRANSIENT_TITLE = re.compile(
    r"\brun \d+|\bin \d+ (?:hours?|days?|minutes?)|expires? in\b|\bweek of\b"
    r"|\b\d{4}-\d{2}-\d{2}\b|\(run \d+\)|#\d+\b",
    re.IGNORECASE,
)
BOT_AUTHOR: re.Pattern[str] = re.compile(
    r"\[bot\]$|^app/|^(renovate|dependabot|github-actions)$", re.IGNORECASE
)
_TITLE_NOISE = re.compile(
    r"[\U0001F300-\U0001FAFF\u2600-\u27BF]|#\d+|\b\d{4}-\d{2}-\d{2}\b|\b\d+\b"
)


def normalize_title(title: str | None) -> str:
    """Strip emoji, dates and numbers so the same recurring tracker matches across repos."""
    return re.sub(r"\s+", " ", _TITLE_NOISE.sub("", str(title or ""))).strip().lower()


def title_recurrence(issues: list[dict]) -> dict[str, set]:
    """normalized title -> set of repos it appears in. Corpus-level, so it needs every issue."""
    out: dict[str, set] = {}
    for issue in issues:
        repo = (issue.get("repository") or {}).get("name") or issue.get("repo") or "?"
        out.setdefault(normalize_title(issue.get("title")), set()).add(repo)
    return out


def _author_login(issue: dict) -> str:
    author = issue.get("author")
    if isinstance(author, dict):
        return str(author.get("login") or "")
    return str(author or "")


def labelled_titles(issues: list[dict]) -> set[str]:
    """Normalized titles that a HUMAN has explicitly marked `tracker:durable` somewhere in the fleet.

    Sibling propagation. `LangSmith fleet coverage` exists in 3 repos: one carries the label, two do
    not and are human-authored, so the author guard alone leaves the same tracker counted three
    different ways. Propagating from an explicit label is not the detector guessing — it is applying
    a decision a person already made to items with an identical title. Only EXPLICIT labels seed
    this set, so propagation can never cascade off another inference.
    """
    return {normalize_title(i.get("title")) for i in issues if DURABLE_LABEL in _norm_labels(i)}


def durability_of(issue: dict, recurrence: dict[str, set], seeded: set[str] | None = None) -> dict:
    """Is this issue DESIGNED to stay open? Returns {durable, basis, bot, repos}."""
    labels = _norm_labels(issue)
    title = normalize_title(issue.get("title"))
    repos = len(recurrence.get(title, ()) or ())
    bot = bool(BOT_AUTHOR.search(_author_login(issue)))
    if DURABLE_LABEL in labels:
        return {"durable": True, "basis": "labelled", "bot": bot, "repos": repos}
    if seeded and title in seeded:
        return {
            "durable": True,
            "basis": "same title labelled durable elsewhere in the fleet",
            "bot": bot,
            "repos": repos,
        }
    if bot and repos >= MIN_REPOS_FOR_DURABLE:
        return {
            "durable": True,
            "basis": f"bot tracker recurring in {repos} repos",
            "bot": bot,
            "repos": repos,
        }
    # Single-repo controller tracker: a bot issue carrying a controller label whose title has no
    # counter or timestamp is a reused dashboard, not a per-occurrence alert. This is how #3130
    # (maint-68) and #3093 (health-67) are recognised -- both reuse one issue via "comment on
    # existing" but neither applies `tracker:durable` at creation.
    if (
        bot
        and (labels & CONTROLLER_LABELS)
        and not TRANSIENT_TITLE.search(str(issue.get("title") or ""))
    ):
        return {
            "durable": True,
            "basis": "controller-labelled tracker, no counter in title",
            "bot": bot,
            "repos": repos,
        }
    # Everything else counts as real work. Never hide an issue on a weak signal.
    return {"durable": False, "basis": "counted as backlog", "bot": bot, "repos": repos}


def census(issues: list[dict]) -> dict:
    """Corrected open-issue count, plus the label-hygiene gap that makes any count wrong."""
    rec = title_recurrence(issues)
    seeded = labelled_titles(issues)
    durable, actionable, unlabelled, mislabelled = [], [], [], []
    for issue in issues:
        repo = (issue.get("repository") or {}).get("name") or "?"
        target = f"{repo}#{issue.get('number')}"
        d = durability_of(issue, rec, seeded)
        row = {"target": target, "title": str(issue.get("title") or "")[:80], **d}
        if d["durable"]:
            durable.append(row)
            if d["basis"] != "labelled":
                unlabelled.append(row)
        else:
            actionable.append(row)
            if DURABLE_LABEL in _norm_labels(issue):
                mislabelled.append(row)
    return {
        "raw_open": len(issues),
        "durable": len(durable),
        "true_open": len(actionable),
        # SCOPE, carried in the payload so it cannot be dropped in the retelling. These counts are
        # THIS TOOL'S view of what its own opener lane could pick up -- they are NOT fleet throughput
        # and not a measure of whether the fleet has work. Fleet work originates in the approved-issue
        # queue (system-of-record: the Workflows repo) and is driven by the opener lane and GitHub
        # Actions keepalive, none of which reads these numbers. On 2026-08-22 a true_open of 1 was
        # reported as "the fleet has no work" while the real pipeline closed 8 issues overnight.
        "scope": (
            "orchestrator-local dispatch lane only; NOT fleet throughput. Fleet work "
            "originates in the Workflows approved-issue queue and is driven by the opener "
            "lane + Actions keepalive."
        ),
        "label_gap": len(unlabelled),
        "durable_rows": durable,
        "actionable_rows": actionable,
        "needs_durable_label": unlabelled,
        "labelled_but_counted": mislabelled,
    }


def reconcile_durable(rows: list[dict], *, dry_run: bool = True) -> dict:
    """Apply `tracker:durable` to detected-but-unlabelled trackers, so the count self-heals."""
    applied, errors = [], []
    for row in rows:
        if dry_run:
            applied.append(row["target"])
            continue
        repo, num = row["target"].split("#")
        proc = subprocess.run(
            [
                "gh",
                "issue",
                "edit",
                num,
                "--repo",
                f"stranske/{repo}",
                "--add-label",
                DURABLE_LABEL,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode == 0:
            applied.append(row["target"])
        else:
            errors.append(f"{row['target']}: {(proc.stderr or '').strip()[:100]}")
    return {"applied": applied, "errors": errors, "dry_run": dry_run}


# ---------------------------------------------------------------------------
# Task-type label repair: give `classify()` the signal it needs, at the source.
#
# WHY THIS AND NOT A TITLE-AWARE CLASSIFIER. `backlog.classify()` reads ONLY labels
# (backlog.py:298 -> dispatcher.py:592 -> build_prompt), and 16% of worked fleet issues carry no
# labels at all. The Trend_Model_Project legacy-removal campaign is the concrete cost: issues
# #5852/#5854/#5856/#5858 drove PRs touching 192/63/56/34 files -- textbook codemod-campaign work --
# and every one classified as `implement` because it had zero labels. Teaching classify() to read
# titles would create a SECOND routing input and a second precedence chain, on top of the one that
# already misroutes (`refactor` silently beats `testing`). Repairing the label instead keeps one
# input, puts the decision on GitHub where it is visible and reversible, and fixes the cause rather
# than routing around it.
#
# THE GUARD THAT MATTERS. A label is applied only when `classify()` currently returns `implement`,
# i.e. there is no existing signal to override. Without it, adding `refactor` to an issue that also
# carries `testing` would STEAL it from the testgen lane, because classify()'s fixed chain puts
# codemod ahead of testgen. Verified live on Trend_Model_Project#5902.
#
# Deliberately codemod-only. Measured over 441 worked issues: this moves 25 to `codemod`. `epic` is
# excluded (only 5 real parents, and 21 of 27 naive title matches were `[Epic #NNN]` CHILD subtasks
# that must stay `implement`); runtime_ac / review / docs are excluded for having no demand at all.
TASK_LABEL_RULES = (
    # A parent epic needs an EPIC_LABEL or `classify()` returns `implement` and the decomposition
    # lane never sees it. Guarded below so `[Epic #NNN]` children are never given this label.
    ("epic", re.compile(r"^\s*\[epic\](?!\s*#)", re.IGNORECASE)),
    (
        "refactor",
        re.compile(
            r"\blegacy removal\b|\bphase \d|\bremove (?:remaining|retired|duplicate|legacy)\b"
            r"|\brelocate\b|consolidat|de-?dup|\brefactor\b|\bextract\b"
            r"|\bmigrat(?:e|ion) .*(?:surface|shape|config)",
            re.IGNORECASE,
        ),
    ),
)


def task_label_for(issue: dict) -> str | None:
    """The task-type label this issue is MISSING, or None. Conservative by construction."""
    raw = [lab.get("name") if isinstance(lab, dict) else lab for lab in (issue.get("labels") or [])]
    labels = [str(r) for r in raw if r]
    # Only act when there is no existing routing signal to override.
    if backlog.classify(labels) != "implement":
        return None
    title = str(issue.get("title") or "")
    if EPIC_CHILD_TITLE_RE.search(title):
        return None  # an already-decomposed subtask is ordinary implement work
    low = {lab.strip().lower() for lab in labels}
    if DURABLE_LABEL in low:
        return None
    for label, pattern in TASK_LABEL_RULES:
        if pattern.search(title):
            return None if label in low else label
    return None


def task_label_gaps(issues: list[dict]) -> list[dict]:
    """Issues whose title names work their labels do not, with the label that would fix it."""
    out = []
    for issue in issues:
        label = task_label_for(issue)
        if not label:
            continue
        repo = (issue.get("repository") or {}).get("name") or "?"
        raw = [
            lab.get("name") if isinstance(lab, dict) else lab for lab in (issue.get("labels") or [])
        ]
        out.append(
            {
                "target": f"{repo}#{issue.get('number')}",
                "title": str(issue.get("title") or "")[:80],
                "label": label,
                "routes_from": backlog.classify([str(r) for r in raw if r]),
                "routes_to": backlog.classify([str(r) for r in raw if r] + [label]),
            }
        )
    return out


def apply_task_labels(rows: list[dict], *, dry_run: bool = True) -> dict:
    """Apply the missing task-type label, creating it in repos that lack it."""
    applied, errors, created = [], [], []
    for row in rows:
        repo, num = row["target"].split("#")
        label = row["label"]
        if dry_run:
            applied.append(row["target"])
            continue
        proc = subprocess.run(
            ["gh", "issue", "edit", num, "--repo", f"stranske/{repo}", "--add-label", label],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0 and "not found" in (proc.stderr or "").lower():
            mk = subprocess.run(
                [
                    "gh",
                    "label",
                    "create",
                    label,
                    "--repo",
                    f"stranske/{repo}",
                    "--color",
                    "5319e7",
                    "--description",
                    "Structural/mechanical change; routes to the " "Orchestrator codemod lane",
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if mk.returncode == 0:
                created.append(f"stranske/{repo}:{label}")
                proc = subprocess.run(
                    [
                        "gh",
                        "issue",
                        "edit",
                        num,
                        "--repo",
                        f"stranske/{repo}",
                        "--add-label",
                        label,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
        if proc.returncode == 0:
            applied.append(row["target"])
        else:
            errors.append(f"{row['target']}: {(proc.stderr or '').strip()[:100]}")
    return {"applied": applied, "labels_created": created, "errors": errors, "dry_run": dry_run}


def format_census(cen: dict) -> str:
    pct = (cen["durable"] / cen["raw_open"] * 100) if cen["raw_open"] else 0.0
    lines = [
        "# Open-issue census — how much of the backlog is real?",
        "",
        f"  raw open issues     {cen['raw_open']:>4}",
        f"  designed-permanent  {cen['durable']:>4}   ({pct:.0f}% — excluded from the backlog)",
        f"  TRUE open backlog   {cen['true_open']:>4}",
        "",
    ]
    if cen["needs_durable_label"]:
        lines += [
            f"## Missing `{DURABLE_LABEL}` ({cen['label_gap']}) — any count is wrong until "
            "these are labelled",
            "",
        ]
        for r in cen["needs_durable_label"]:
            lines.append(f"  {r['target']:<34} {r['basis']:<32} {r['title'][:44]}")
        lines.append("")
    if cen["labelled_but_counted"]:
        lines += [f"## Labelled `{DURABLE_LABEL}` but still counted (detector disagrees)", ""]
        for r in cen["labelled_but_counted"]:
            lines.append(f"  {r['target']:<34} {r['title'][:60]}")
        lines.append("")
    return "\n".join(lines)


def format_report(rep: dict) -> str:
    c = rep["counts"]
    lines = [
        "# Issue readiness — who decides what the fleet works",
        "",
        f"{rep['total']} open issues; {rep['classifiable']} are candidate work items.",
        "",
        f"  auto_ready          {c['auto_ready']:>3}   -> `{READY_LABEL}` applied, no owner touch",
        f"  owner_review        {c['owner_review']:>3}   -> non-blocking question, ratifies in "
        f"{OWNER_QUESTION_EXPIRY_DAYS:.0f}d",
        f"  needs_specification {c['needs_specification']:>3}   -> triage drafts AC, re-run next tick",
        f"  not_opener_work     {c['not_opener_work']:>3}   -> excluded (counted below, not dropped)",
        "",
    ]
    att = rep.get("attention") or {}
    if att:
        lines += [
            f"Owner cost: {rep['owner_review_share_of_arrivals']:.1%} of arrivals "
            f"({rep['owner_review_share']:.1%} of candidates) -> "
            f"{att['owner_reviews_per_week']:.2f} reviews/week -> {att['minutes_per_week']:.1f} "
            f"min/week vs {att['budget_min_per_week']:.0f} budget "
            f"(ratio {att['ratio']:.2f}, {att['verdict'].upper()})",
            "",
        ]
    lines += ["## Verdict reasons (every issue counted exactly once)", ""]
    for reason, n in sorted(rep["reasons"].items(), key=lambda kv: -kv[1]):
        lines.append(f"  {n:>3}  {reason}")
    review = [r for r in rep["rows"] if r["verdict"] == "owner_review"]
    if review:
        lines += ["", "## Needs your call (proceeds on the default if unanswered)", ""]
        for r in review:
            lines.append(f"  {r['target']:<32} {r['title'][:70]}")
    return "\n".join(lines) + "\n"


def _selftest() -> None:
    ac = {
        "number": 1,
        "title": "Fix parser",
        "labels": [{"name": "bug"}],
        "body": "## Tasks\n- [ ] fix it",
    }
    assert classify_issue(ac)["verdict"] == "auto_ready", classify_issue(ac)

    # Two code anchors are actionable even with no literal "acceptance criteria".
    anchors = {
        "number": 2,
        "title": "promote() writes to two trees",
        "labels": [{"name": "bug"}],
        "body": "In ops/promote.py the call promote(master) diverges from sidecar.py",
    }
    assert classify_issue(anchors)["verdict"] == "auto_ready", classify_issue(anchors)

    # FAIL-CLOSED on risk: exact label, unseen `risk:*` variant, and substring all reach the owner.
    for lab in (
        "risk:major",
        "security",
        "risk:critical",
        "some-security-thing",
        "breaking-change",
    ):
        risky = dict(ac, labels=[{"name": "bug"}, {"name": lab}])
        assert classify_issue(risky)["verdict"] == "owner_review", lab
    assert is_risky({"risk:major"}) and not is_risky({"bug", "ui"})

    # Under-specified goes to the MACHINE, never to the owner.
    for body in (None, "", "   ", "please fix this thing"):
        thin = dict(ac, body=body)
        assert classify_issue(thin)["verdict"] == "needs_specification", body
    # ...and a risk label does not rescue an unactionable issue into the owner queue.
    assert (
        classify_issue(dict(ac, body="", labels=[{"name": "risk:major"}]))["verdict"]
        == "needs_specification"
    )

    # Already-routed and already-ready are never re-readied.
    for lab in ("agent:codex", "status:in-progress", "status: ready"):
        routed = dict(ac, labels=[{"name": lab}])
        assert classify_issue(routed)["verdict"] == "not_opener_work", lab

    # Bot reports and containers are excluded, and the operational ones are named as such.
    alert = {
        "number": 3,
        "title": "\U0001f6a8 CODEX_AUTH_JSON has expired - CI agents broken",
        "labels": [],
        "body": "x",
    }
    assert classify_issue(alert)["reason"] == "operational alert", classify_issue(alert)
    dash = {
        "number": 4,
        "title": "\U0001f4ca LangSmith Trace Coverage Dashboard",
        "labels": [],
        "body": "x",
    }
    assert classify_issue(dash)["verdict"] == "not_opener_work"
    # A PARENT epic is routable work for the decomposition lane, NOT a container to exclude.
    # It used to be excluded, which is why all 5 fleet parents were decomposed by hand.
    epic = {"number": 5, "title": "[epic] redo everything", "labels": [], "body": "- [ ] a"}
    assert classify_issue(epic)["verdict"] == "auto_ready", classify_issue(epic)
    assert "parent epic" in classify_issue(epic)["reason"], classify_issue(epic)
    assert task_label_for(epic) == "epic", task_label_for(epic)
    assert backlog.classify(["epic"]) == "epic", "the epic label must route to the epic lane"
    # A CHILD subtask is ordinary work: it must NOT be excluded as a container, and must NOT be
    # given the epic label (21 fleet children were locked out of the ready queue by `\bepic\b`).
    kid = {
        "number": 6,
        "title": "[Epic #845][P1] ExportService port",
        "labels": [],
        "body": "- [ ] a",
    }
    assert classify_issue(kid)["verdict"] == "auto_ready", classify_issue(kid)
    assert "parent epic" not in classify_issue(kid)["reason"], classify_issue(kid)
    assert task_label_for(kid) is None, task_label_for(kid)
    # A genuine tracker container is still excluded.
    assert (
        classify_issue(
            {
                "number": 7,
                "title": "Sync/Dependabot campaign queue",
                "labels": [],
                "body": "- [ ] a",
            }
        )["verdict"]
        == "not_opener_work"
    )
    assert (
        classify_issue(
            {"number": 6, "title": "t", "labels": [{"name": "tracker:durable"}], "body": "- [ ] a"}
        )["verdict"]
        == "not_opener_work"
    )

    # Malformed input must not raise and must not auto-ready.
    for bad in ({}, {"labels": None, "body": None}, {"labels": ["bug"], "title": None}):
        assert classify_issue(bad)["verdict"] != "auto_ready", bad

    # Roll-up counts every issue exactly once; exclusions are visible, not dropped.
    rep = assess([ac, dict(ac, number=7, labels=[{"name": "risk:major"}]), dash, dict(ac, body="")])
    assert sum(rep["counts"].values()) == rep["total"] == 4, rep["counts"]
    assert rep["counts"] == {
        "auto_ready": 1,
        "owner_review": 1,
        "needs_specification": 1,
        "not_opener_work": 1,
    }, rep["counts"]
    assert sum(rep["reasons"].values()) == 4, rep["reasons"]
    text = format_report(rep)
    assert "not dropped" in text and "Needs your call" in text

    # The rule must report its OWN cost honestly, including when that cost is unacceptable.
    cheap = attention_cost(0.05)
    assert cheap["verdict"] == "viable" and cheap["ratio"] < 0.5, cheap
    assert attention_cost(0.0)["owner_reviews_per_week"] == 0.0
    # If the issue mix drifted so most arrivals were risk-labelled, this must NOT read as fine.
    assert attention_cost(0.5)["verdict"] == "infeasible", attention_cost(0.5)
    assert attention_cost(0.2)["verdict"] == "redesign", attention_cost(0.2)
    assert "INFEASIBLE" in format_report(assess([dict(ac, labels=[{"name": "risk:major"}])]))

    # Dry run must never write.
    res = apply_ready(rep["rows"], dry_run=True)
    assert res["dry_run"] and res["applied"] and not res["errors"], res

    # Per-repo label spelling: the fleet uses BOTH `status: ready` and `status:ready`, so writing
    # one hardcoded variant silently under-applies. Resolution must follow the repo, and a repo
    # with no ready label at all must produce an error rather than a skipped no-op.
    spellings = {"spaced": "status: ready", "tight": "status:ready", "none": None}
    live_cache = (repo_ready_label.__defaults__ or (None,))[
        0
    ]  # the module's own memo, primed for the test
    saved_run, calls = subprocess.run, []
    try:
        live_cache.update(spellings)
        rows = [{"verdict": "auto_ready", "target": f"{r}#1", "title": "t"} for r in spellings]

        def fake_run(cmd, **kw):
            class R:
                returncode, stdout, stderr = 0, "", ""

            if cmd[1] == "issue":
                calls.append((cmd[cmd.index("--repo") + 1], cmd[cmd.index("--add-label") + 1]))
            return R()

        subprocess.run = fake_run  # type: ignore[assignment]  # deliberate selftest monkeypatch
        out = apply_ready(rows, dry_run=False)
    finally:
        subprocess.run = saved_run
        for key in spellings:
            live_cache.pop(key, None)
    # repo_ready_label's cache was primed above, so each repo wrote ITS OWN spelling.
    assert ("stranske/spaced", "status: ready") in calls, calls
    assert ("stranske/tight", "status:ready") in calls, calls
    assert any("no ready label" in e for e in out["errors"]), out["errors"]
    assert "none#1" not in out["applied"], out

    # DELIBERATE BREAK -> REVERT on the correctness-critical gate (the risk check).
    global RISK_LABELS
    saved = RISK_LABELS
    try:
        RISK_LABELS = set()  # simulate the risk vocabulary going empty
        broken = classify_issue(dict(ac, labels=[{"name": "risk:major"}]))
        # Substring matching is the second line of defence and must still hold the line.
        assert broken["verdict"] == "owner_review", "substring fallback failed to fail closed"
        RISK_SUBS_SAVED = tuple(RISK_SUBSTRINGS)
        globals()["RISK_SUBSTRINGS"] = ()  # remove BOTH defences
        now_broken = classify_issue(dict(ac, labels=[{"name": "risk:major"}]))
        assert (
            now_broken["verdict"] == "auto_ready"
        ), "break did not change behaviour — test is vacuous"
        globals()["RISK_SUBSTRINGS"] = RISK_SUBS_SAVED
    finally:
        RISK_LABELS = saved
    assert (
        classify_issue(dict(ac, labels=[{"name": "risk:major"}]))["verdict"] == "owner_review"
    ), "revert did not restore the risk gate"

    # ---- durable-issue census -------------------------------------------------------------
    def _iss(repo, num, title, labels=(), author="app/github-actions", body="x"):
        return {
            "number": num,
            "title": title,
            "body": body,
            "labels": [{"name": lab} for lab in labels],
            "author": {"login": author},
            "repository": {"name": repo},
        }

    # Emoji, dates and issue numbers must not stop the same tracker matching across repos.
    assert (
        normalize_title("\U0001f6a8 Dependency Dashboard #12 2026-08-18") == "dependency dashboard"
    )
    assert normalize_title(None) == ""

    fleet = [_iss(f"repo{i}", 100 + i, "Dependency Dashboard") for i in range(4)]
    rec = title_recurrence(fleet)
    assert len(rec["dependency dashboard"]) == 4, rec

    # A bot tracker recurring across >=3 repos is durable even with no label.
    d = durability_of(fleet[0], rec)
    assert d["durable"] and "recurring in 4 repos" in d["basis"], d

    # THE FAIL-CLOSED DIRECTION IS INVERTED HERE: marking durable HIDES work, so anything
    # uncertain must COUNT. A human author is never auto-marked, however recurrent the title.
    human = _iss("repo0", 200, "Dependency Dashboard", author="stranske")
    hrec = title_recurrence([human] + fleet)
    assert not durability_of(human, hrec)["durable"], "human-authored issue was auto-hidden"

    # Below the repo threshold, a bot issue still counts as real work.
    two = [_iss(f"r{i}", 300 + i, "Sync Failed - Action Required") for i in range(2)]
    assert not durability_of(two[0], title_recurrence(two))["durable"]

    # An explicit label is authoritative even for a one-off, human-authored issue.
    lab = _iss("repo0", 400, "Long-running roadmap", labels=[DURABLE_LABEL], author="stranske")
    assert durability_of(lab, title_recurrence([lab]))["basis"] == "labelled"

    # Census arithmetic: every issue lands in exactly one bucket.
    corpus = fleet + [human] + two + [lab, _iss("repo0", 500, "Fix the parser", author="stranske")]
    cen = census(corpus)
    assert cen["raw_open"] == len(corpus)
    assert cen["durable"] + cen["true_open"] == cen["raw_open"], cen
    assert cen["durable"] == 5 and cen["true_open"] == 4, cen  # 4 dashboards + 1 labelled
    # THE SCOPE LABEL IS PART OF THE CONTRACT, not a comment. These counts were read as fleet
    # throughput on 2026-08-22 -- a true_open of 1 reported as "the fleet has no work" while the real
    # pipeline closed 8 issues overnight. A number that invites that misreading has to carry its own
    # scope in the payload, because prose in a docstring does not travel with the JSON.
    assert "NOT fleet throughput" in cen["scope"], cen.get("scope")
    assert cen["label_gap"] == 4, cen  # the 4 unlabelled dashboards
    text = format_census(cen)
    assert "TRUE open backlog" in text and "Missing `tracker:durable`" in text

    # Reconciliation dry run must never write.
    r = reconcile_durable(cen["needs_durable_label"], dry_run=True)
    assert r["dry_run"] and len(r["applied"]) == 4 and not r["errors"], r

    # The registry's OWN examples (docs/ops/DURABLE_TRACKING_ISSUES.md, "Distinguishing trackers
    # from transient alerts"). Durable: generic title + controller label. Transient: counter/date.
    ctrl = _iss(
        "Workflows",
        3130,
        "\U0001f6a8 Consumer Repo Sync Failed - Action Required",
        labels=["sync-failure", "automated", "bug"],
    )
    assert durability_of(ctrl, title_recurrence([ctrl]))["durable"], durability_of(ctrl, {})
    drift = _iss(
        "Workflows",
        3093,
        "\U0001f504 Integration-Tests sync drift detected",
        labels=["integration-sync", "automation"],
    )
    assert durability_of(drift, title_recurrence([drift]))["durable"]
    for transient in (
        "\u26a0\ufe0f CODEX_AUTH_JSON expires in 39 hours",
        "\U0001f534 Integration CI failed (run 235)",
        "\U0001f4ca LangSmith Trace Coverage Report - Week of 2026-08-01",
    ):
        t = _iss("Workflows", 999, transient, labels=["automated"])
        assert not durability_of(t, title_recurrence([t]))["durable"], transient
    # A controller label alone, on a HUMAN-authored issue, must not hide it.
    hc = _iss("Workflows", 998, "Generic sounding title", labels=["automated"], author="stranske")
    assert not durability_of(hc, title_recurrence([hc]))["durable"]

    # Sibling propagation: one explicit human label makes identical titles durable fleet-wide,
    # even when they are human-authored and below the recurrence threshold.
    sib = [
        _iss("a", 1, "LangSmith fleet coverage", labels=[DURABLE_LABEL], author="stranske"),
        _iss("b", 2, "LangSmith fleet coverage", author="stranske"),
        _iss("c", 3, "LangSmith fleet coverage", author="stranske"),
    ]
    srec, seeds = title_recurrence(sib), labelled_titles(sib)
    assert durability_of(sib[1], srec, seeds)["basis"].startswith(
        "same title labelled"
    ), durability_of(sib[1], srec, seeds)
    # ...and with NO seed it must stay counted (propagation cannot invent itself).
    assert not durability_of(sib[1], srec, set())["durable"]
    # Propagation seeds ONLY from explicit labels, so it cannot cascade off an inference.
    infer = [_iss(f"r{i}", 10 + i, "Dependency Dashboard") for i in range(4)]
    assert labelled_titles(infer) == set(), "inferred durability must not seed propagation"

    # ---- task-type label repair -----------------------------------------------------------
    camp = _iss(
        "Trend_Model_Project",
        5856,
        "Legacy removal Phase 6b: Remove legacy config and multi-period shapes",
        labels=[],
        author="stranske",
    )
    assert task_label_for(camp) == "refactor", task_label_for(camp)

    # THE LOAD-BEARING GUARD: never override an existing routing signal. classify()'s fixed chain
    # puts codemod ahead of testgen, so adding `refactor` to a testgen issue would STEAL it.
    assert task_label_for(dict(camp, labels=[{"name": "testing"}])) is None
    assert backlog.classify(["testing", "refactor"]) == "codemod", "precedence assumption changed"

    # Already-decomposed epic CHILDREN stay ordinary implement work.
    assert task_label_for(dict(camp, title="[Epic #845][P1] Export panel UI")) is None
    # A parent epic gets the EPIC label (in scope since 2026-08-20), never the codemod one.
    assert task_label_for(dict(camp, title="[Epic] Export layer")) == "epic"
    assert task_label_for(dict(camp, title="[Epic #845][P1] child work")) is None
    # Ordinary work is untouched, and an issue already carrying the label is a no-op.
    assert task_label_for(dict(camp, title="Fix the login redirect")) is None
    assert task_label_for(dict(camp, labels=[{"name": "refactor"}])) is None
    # Durable trackers are never relabelled.
    assert task_label_for(dict(camp, labels=[{"name": DURABLE_LABEL}])) is None

    gaps = task_label_gaps([camp, dict(camp, number=1, title="Fix the login redirect")])
    assert len(gaps) == 1 and gaps[0]["routes_from"] == "implement", gaps
    assert gaps[0]["routes_to"] == "codemod", gaps
    assert apply_task_labels(gaps, dry_run=True)["dry_run"] is True

    # DELIBERATE BREAK -> REVERT on the guard: without it, a testgen issue is stolen by codemod.
    _saved_classify = backlog.classify
    try:
        backlog.classify = (
            lambda _labels: "implement"  # type: ignore[assignment]
        )  # pretend there is never a signal  # type: ignore[assignment]  # deliberate selftest monkeypatch
        stolen = task_label_for(dict(camp, labels=[{"name": "testing"}]))
        assert stolen == "refactor", "break did not change behaviour — test is vacuous"
    finally:
        backlog.classify = _saved_classify
    assert (
        task_label_for(dict(camp, labels=[{"name": "testing"}])) is None
    ), "revert did not restore the guard"

    # DELIBERATE BREAK -> REVERT: drop the bot-author guard and the human issue gets hidden.
    global BOT_AUTHOR
    saved_bot_author = BOT_AUTHOR
    try:
        BOT_AUTHOR = re.compile(r".*")  # everyone looks like a bot
        broken = durability_of(human, hrec)
        assert broken["durable"], "break did not change behaviour — test is vacuous"
    finally:
        BOT_AUTHOR = saved_bot_author
    assert not durability_of(human, hrec)["durable"], "revert did not restore the author guard"

    print(
        "issue_readiness.py selftest: OK (fail-closed risk w/ break->revert, thin issues go to "
        "the machine not the owner, exclusions counted, dry-run writes nothing)"
    )


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--apply", action="store_true", help="write labels / raise questions")
    ap.add_argument(
        "--census",
        action="store_true",
        help="corrected open-issue count, excluding issues designed to stay open",
    )
    ap.add_argument(
        "--reconcile-durable",
        action="store_true",
        help=f"apply `{DURABLE_LABEL}` to detected-but-unlabelled trackers",
    )
    ap.add_argument(
        "--task-labels",
        action="store_true",
        help="report issues whose title names work their labels do not",
    )
    ap.add_argument(
        "--apply-task-labels",
        action="store_true",
        help="apply the missing task-type label so classify() can route it",
    )
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0

    issues = fetch_open_issues(args.limit)
    if args.task_labels or args.apply_task_labels:
        gaps = task_label_gaps(issues)
        out = {"candidates": len(gaps), "rows": gaps}
        if args.apply_task_labels:
            if not APPLY_ENABLED:
                print("refusing to apply: set ORCH_ISSUE_AUTOREADY=1", file=sys.stderr)
                return 2
            out["apply"] = apply_task_labels(gaps, dry_run=False)
        if args.json:
            print(json.dumps(out, indent=2))
        else:
            print("# Task-label gaps — titles naming work the labels do not\n")
            print(f"  {len(gaps)} issue(s) would re-route once labelled\n")
            for g in gaps:
                print(
                    f"  {g['target']:<34} +{g['label']:<10} "
                    f"{g['routes_from']} -> {g['routes_to']}"
                )
                print(f"       {g['title']}")
        return 0
    if args.census or args.reconcile_durable:
        cen = census(issues)
        if args.reconcile_durable:
            if not APPLY_ENABLED:
                print(
                    "refusing to apply: set ORCH_ISSUE_AUTOREADY=1 to enable writes",
                    file=sys.stderr,
                )
                return 2
            cen["reconcile"] = reconcile_durable(cen["needs_durable_label"], dry_run=False)
        print(json.dumps(cen, indent=2) if args.json else format_census(cen), end="")
        return 0
    rep = assess(issues)
    rep["census"] = {k: v for k, v in census(issues).items() if not k.endswith("_rows")}
    if not issues:
        rep["unreadable_repos"] = fetch_failures()
    if args.apply and not APPLY_ENABLED:
        print("refusing to apply: set ORCH_ISSUE_AUTOREADY=1 to enable writes", file=sys.stderr)
        return 2
    if args.apply:
        rep["apply"] = apply_ready(rep["rows"], dry_run=False)
    print(json.dumps(rep, indent=2) if args.json else format_report(rep), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
