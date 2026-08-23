#!/usr/bin/env python3
"""backlog.py — discover actionable work across the supported repos -> backlog.json.

Grounded in stranske/Workflows/docs/ops/REPO_REVIEW_PROCESS.md and the project
CLAUDE.md incident (2026-04-29): an EMPTY approved-issue-queue does NOT mean "no
work" — the opener's selection space also includes already-published open GitHub
issues from prior cycles. So this discovers:

  - OPENER (lane=opener): open issues marked agent-ready (status: ready) that are
    NOT already referenced by any open PR (so we don't redo in-flight work).
  - CLOSER (lane=closer): in-flight agent PRs (agent:* label) across all repos.

Scoped-blockers (sentinel .stop.scoped_blockers) are excluded — same as the lanes'
own discovery filters. task_type is classified from labels (cross-repo/sync-manifest =>
cross_repo; epic/planning => epic; codemod/refactor/structural/campaign => codemod;
runtime AC/verification spec => runtime_ac; tests/coverage => testgen;
chore/deps/docs/style => mechanical; default => implement);
review/polish come from later verifier signals (TODO). Issue/PR bodies are retained so shadow
triage roles can judge underspecification from more than titles and labels. The router consumes
backlog.json; it claims targets, so this producer does NOT need to dedup against in-flight claims.

`--selftest` is network-free (mocked gh). `--live` queries gh and writes
~/.codex/handoff/backlog.json.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HANDOFF = Path(os.environ.get("HANDOFF_DIR", Path.home() / ".codex" / "handoff"))
SENTINEL = HANDOFF / "lane-handoff.json"
BACKLOG_JSON = HANDOFF / "backlog.json"

SUPPORTED_REPOS = [
    "stranske/Workflows",
    "stranske/Travel-Plan-Permission",
    "stranske/Trend_Model_Project",
    "stranske/Portable-Alpha-Extension-Model",
    "stranske/Counter_Risk",
    "stranske/Manager-Database",
    "stranske/Inv-Man-Intake",
    "stranske/Pension-Data",
    "stranske/Ready",
    "stranske/trip-planner",
    "stranske/learning-management-system",
    "stranske/Fine-Art-Archive",
]  # 12 repos; runtime authority is ~/.codex/bin/handoff.sh SUPPORTED_REPOS. `Ready` was absent
# here until 2026-08-18, so its issues were invisible to the backlog entirely.

READY_LABELS = {"status: ready", "status:ready", "agent-ready"}
# label (lowercased) -> task_type; absent => "implement"
MECHANICAL_LABELS = {
    "dependencies",
    "dependency",
    "chore",
    "documentation",
    "docs",
    "style",
    "formatting",
    "lint",
    "ci",
}
TESTGEN_LABELS = {"test", "tests", "testing", "coverage", "testgen", "unit-tests"}
EPIC_LABELS = {"epic", "planning", "decomposition", "multi-issue", "roadmap", "large-goal"}
CODEMOD_LABELS = {"codemod", "refactor", "refactoring", "structural", "bulk-change", "campaign"}
CROSS_REPO_LABELS = {
    "cross-repo",
    "multi-repo",
    "coordinated-change",
    "consumer-sync",
    "sync-manifest",
    "dependency-graph",
    "contract-change",
}
RUNTIME_AC_LABELS = {
    "runtime-ac",
    "runtime-verification",
    "acceptance-criteria",
    "verification-spec",
    "verification-plan",
    "ac-checks",
    "runtime-checks",
}
SOURCE_ISSUE_BRANCH_RE = re.compile(
    r"(?:^|[/_-])(?:issue|source|source-issue|src|gh)-?(\d+)(?:$|[/_-])",
    re.IGNORECASE,
)


def _labels(obj: dict) -> list[str]:
    out = []
    for lbl in obj.get("labels", []) or []:
        out.append(lbl.get("name", "") if isinstance(lbl, dict) else str(lbl))
    return out


def classify(labels: list[str]) -> str:
    low = {lb.strip().lower() for lb in labels}
    normalized = low | {lb.split(":", 1)[1].strip() for lb in low if ":" in lb}
    if normalized & CROSS_REPO_LABELS:
        return "cross_repo"
    if normalized & EPIC_LABELS:
        return "epic"
    if normalized & CODEMOD_LABELS:
        return "codemod"
    if normalized & RUNTIME_AC_LABELS:
        return "runtime_ac"
    if normalized & TESTGEN_LABELS:
        return "testgen"
    if normalized & MECHANICAL_LABELS:
        return "mechanical"
    return "implement"


def _is_ready(labels: list[str]) -> bool:
    return any(lb.strip().lower() in {r.lower() for r in READY_LABELS} for lb in labels)


def _has_agent_label(labels: list[str]) -> bool:
    return any(lb.strip().lower().startswith("agent:") for lb in labels)


def _pr_issue_refs(repo: str, pr: dict) -> set[int]:
    """Issue numbers an open PR is explicitly covering in this repo."""
    refs: set[int] = set()
    for ref in pr.get("closingIssuesReferences", []) or []:
        if isinstance(ref, dict) and ref.get("number"):
            refs.add(int(ref["number"]))
    body = pr.get("body", "") or ""
    for pattern in (
        r"<!--\s*meta:issue:(\d+)\s*-->",
        r"<!--\s*meta:source[_-]?issue:(\d+)\s*-->",
        r"\bSource Issue #(\d+)\b",
        r"\bSource\s*:?\s*#(\d+)\b",
        r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b",
        rf"https://github\.com/{re.escape(repo)}/issues/(\d+)\b",
    ):
        for match in re.finditer(pattern, body, flags=re.IGNORECASE):
            refs.add(int(match.group(1)))
    branch = pr.get("headRefName", "") or ""
    for match in SOURCE_ISSUE_BRANCH_RE.finditer(branch):
        refs.add(int(match.group(1)))
    return refs


def _issue_targets(repo: str, pr: dict) -> set[str]:
    return {f"{repo}#{issue_number}" for issue_number in _pr_issue_refs(repo, pr)}


def _closer_preference_key(pr: dict) -> tuple:
    """Sort key for duplicate closer PRs that claim the same source issue."""
    try:
        number = int(pr.get("number", 0) or 0)
    except (TypeError, ValueError):
        number = 0
    return (
        bool(pr.get("isDraft")),
        -len(pr.get("closingIssuesReferences", []) or []),
        number,
    )


def _blocker_expiry_ts(entry) -> int | None:
    """`expires_at` as epoch seconds, or None when absent/unparseable."""
    raw = (entry or {}).get("expires_at") if isinstance(entry, dict) else None
    if raw in (None, ""):
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    try:
        import datetime as _dt

        return int(_dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def scoped_blocker_entries() -> dict:
    """Raw scoped-blocker map from the sentinel ({} when unreadable)."""
    try:
        data = json.loads(SENTINEL.read_text())
        return (data.get("stop", {}) or {}).get("scoped_blockers", {}) or {}
    except Exception:
        return {}


def expired_scoped_blockers(now: int | None = None) -> dict:
    """Blockers past their own `expires_at` — i.e. ones that should no longer block."""
    current = int(now if now is not None else time.time())
    out = {}
    for key, entry in scoped_blocker_entries().items():
        ts = _blocker_expiry_ts(entry)
        if ts is not None and ts < current:
            out[str(key)] = entry
    return out


def load_scoped_blockers(now: int | None = None) -> set[str]:
    """Targets the lanes have scope-blocked (excluded from discovery) — EXPIRY HONOURED.

    ROOT CAUSE FIX 2026-08-10. Blockers are written with `expires_at` and `await_human`, so the
    design always intended them to lapse — but this function returned every key regardless, so an
    expired blocker blocked forever. Measured at the fix: 24 of 30 blockers were past their own
    expiry, the oldest by nearly two months, and between them they had emptied the backlog
    completely — no opener candidates, therefore no experiments, therefore no cross-evaluation
    (`review`) runs since 2026-07-15. The data said "expired"; only the code disagreed.

    A blocker with no parseable `expires_at` still blocks (fail-safe: an unbounded block is a
    deliberate one). Expiry is surfaced, not silent — see `raise_expired_blocker_questions`.
    """
    current = int(now if now is not None else time.time())
    live = set()
    for key, entry in scoped_blocker_entries().items():
        ts = _blocker_expiry_ts(entry)
        if ts is None or ts >= current:
            live.add(str(key))
    return live


def raise_expired_blocker_questions(now: int | None = None) -> list[dict]:
    """Tell the owner, once per blocker, that a human-awaited block has lapsed.

    The owner-question protocol is the right channel: non-blocking, deduped per scope, and
    auto-ratifying at its own expiry, so this can never become a queue. Without it an expired
    blocker would simply start letting work through with nobody told — which is the silent-state-
    change failure this whole area keeps producing. Only `await_human` blockers are raised;
    a machine-reason block lapsing needs no decision from the owner.
    """
    raised = []
    try:
        import feedback
    except Exception:
        return raised
    for target, entry in expired_scoped_blockers(now).items():
        if not (entry or {}).get("await_human"):
            continue
        reason = str((entry or {}).get("reason") or "").strip()
        try:
            result = feedback.record_owner_question(
                f"Scope-block on {target} has expired and work may now proceed. "
                f"Original reason: {reason[:280]}",
                "the block has lapsed, so the lane will treat this target as eligible again",
                target=target,
                repo=target.split("#", 1)[0],
                expires_days=7.0,
            )
        except Exception:
            continue
        # Count only NEW questions. The store dedupes per scope, but counting attempts would report
        # the same number every tick and read as spam when nothing new had happened.
        if not result.get("deduped"):
            raised.append(result)
    return raised


def build_backlog(repos, fetch_issues, fetch_prs, scoped: set[str] | None = None) -> list[dict]:
    """Pure planner over injected fetchers — fully unit-testable.

    fetch_issues(repo) -> [{number, labels, title, body}]; open issues.
    fetch_prs(repo)    -> [{number, labels, title, body, closingIssuesReferences:[{number}]}]; open PRs.
    """
    scoped = scoped or set()
    items: list[dict] = []
    referenced_issue_targets: set[str] = set()
    issues_by_repo = {repo: (fetch_issues(repo) or []) for repo in repos}
    prs_by_repo = {repo: (fetch_prs(repo) or []) for repo in repos}
    open_issue_targets = {
        f"{repo}#{issue['number']}"
        for repo, issues in issues_by_repo.items()
        for issue in issues
        if issue.get("number") is not None
    }
    closer_candidates: list[tuple[str, dict, list[str], set[str]]] = []
    stale_source_pr_targets: set[str] = set()
    by_source_issue: dict[str, list[tuple[str, dict, list[str], set[str]]]] = {}

    # Pass 1: collect issues covered by open PRs. Only agent PRs become closers, but
    # any open closing PR should suppress duplicate opener dispatch.
    for repo in repos:
        for pr in prs_by_repo.get(repo, []):
            labels = _labels(pr)
            source_targets = _issue_targets(repo, pr)
            referenced_issue_targets.update(source_targets)
            if not _has_agent_label(labels):
                continue
            target = f"{repo}#{pr['number']}"
            if target in scoped:
                continue
            candidate = (repo, pr, labels, source_targets)
            closer_candidates.append(candidate)
            for source_target in source_targets:
                by_source_issue.setdefault(source_target, []).append(candidate)
            if source_targets and not (source_targets & open_issue_targets):
                stale_source_pr_targets.add(target)

    # target -> issue, so a closer item can carry its SOURCE issue's labels (see below).
    issues_by_target: dict[str, dict] = {}
    for repo in repos:
        for issue in issues_by_repo.get(repo, []):
            issues_by_target[f"{repo}#{issue['number']}"] = issue

    canonical_by_source: dict[str, str] = {}
    for source_target, candidates in by_source_issue.items():
        winner = min(candidates, key=lambda row: _closer_preference_key(row[1]))
        canonical_by_source[source_target] = f"{winner[0]}#{winner[1]['number']}"

    for repo, pr, labels, source_targets in closer_candidates:
        target = f"{repo}#{pr['number']}"
        if target in stale_source_pr_targets:
            continue
        if source_targets and any(
            canonical_by_source.get(source_target) not in (None, target)
            for source_target in source_targets
        ):
            continue
        # SOURCE-ISSUE LABELS, carried alongside (never merged into) the PR's own labels.
        #
        # Risk metadata lives on ISSUES; the closer lane reads PR labels; and no PR in the fleet
        # carries a `risk:*` label — 0 across every repo checked 2026-08-20. So
        # `adversarial.high_stakes_reason()` could never see a risk label on the lane it is
        # restricted to, which made the adversarial panel unreachable for risky work even after
        # `risk:major` was added to its vocabulary. Travel-Plan-Permission#1429 ("Policy fails
        # open") and #1436 ("Audit record and state change are not atomic") both merged unreviewed
        # for exactly this reason.
        #
        # Kept as a SEPARATE key so `classify()`, `_has_agent_label`, and every other labels
        # consumer see exactly what they saw before; only the high-stakes check reads it.
        source_labels: list[str] = []
        for source_target in sorted(source_targets or ()):
            src_issue = issues_by_target.get(source_target)
            if src_issue:
                source_labels.extend(_labels(src_issue))
        items.append(
            {
                "target": target,
                "task_type": "implement",
                "lane": "closer",
                "labels": labels,
                "source_labels": sorted(set(source_labels)),
                "title": pr.get("title", "") or "",
                "body": pr.get("body", "") or "",
            }
        )

    # Pass 2: agent-ready open issues NOT already worked by an open PR -> opener candidates.
    for repo in repos:
        for issue in issues_by_repo.get(repo, []):
            labels = _labels(issue)
            if not _is_ready(labels):
                continue
            target = f"{repo}#{issue['number']}"
            if target in scoped or target in referenced_issue_targets:
                continue
            items.append(
                {
                    "target": target,
                    "task_type": classify(labels),
                    "lane": "opener",
                    "labels": labels,
                    "title": issue.get("title", "") or "",
                    "body": issue.get("body", "") or "",
                }
            )

    return items


# --- live gh fetchers -------------------------------------------------------
def _gh_json(args: list[str]) -> list:
    try:
        out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            return []
        return json.loads(out.stdout or "[]")
    except Exception:
        return []


def live_fetch_issues(repo: str) -> list:
    return _gh_json(
        [
            "issue",
            "list",
            "-R",
            repo,
            "--state",
            "open",
            "-L",
            "1000",
            "--json",
            "number,labels,title,body",
        ]
    )


def live_fetch_prs(repo: str) -> list:
    return _gh_json(
        [
            "pr",
            "list",
            "-R",
            repo,
            "--state",
            "open",
            "-L",
            "50",
            "--json",
            "number,labels,title,body,closingIssuesReferences,headRefName,isDraft",
        ]
    )


# ---------------------------------------------------------------------------
# SCOPE, carried in the artifact rather than in prose. `items` is what THIS TOOL's dispatch lane
# discovered -- never the fleet's workload. Fleet work originates in the Workflows approved-issue queue
# and is driven by the opener lane + Actions keepalive, none of which read this file. So an empty or
# 1-item list does NOT mean "the fleet has no work": that inference was made on 2026-04-29 (recorded
# in the workspace CLAUDE.md) and again on 2026-08-22, when a 1-item list was reported as fleet
# starvation while the real pipeline closed 8 issues overnight. A docstring does not travel with JSON.
SCOPE_NOTE = (
    "orchestrator-local dispatch lane only; NOT fleet workload. See "
    "Workflows/docs/ops/REPO_REVIEW_PROCESS.md for where fleet work comes from."
)


def build_payload(items: list, live_blockers, expired, raised) -> dict:
    """The backlog.json artifact. Split out of main() so the scope label is TESTABLE.

    It was first "tested" by grepping this file for the label -- an assertion whose own literal
    satisfied the grep, so it passed against the label's removal. Build the real payload instead.
    """
    return {
        "generated_at": int(time.time()),
        "items": items,
        "scope": SCOPE_NOTE,
        # Reported every run so an empty backlog is never mistaken for "no work exists": these are
        # the counts that explain WHY it is empty (the 2026-08-10 stall took an hour to diagnose
        # precisely because this was invisible).
        "scoped_blockers_live": len(live_blockers),
        "scoped_blockers_expired": len(expired),
        "owner_questions_raised": len(raised),
    }


def _selftest() -> None:
    repos = ["o/A", "o/B"]
    issues = {
        "o/A": [
            {
                "number": 1,
                "labels": [{"name": "status: ready"}],
                "body": "Do thing.",
            },  # implement/opener
            {
                "number": 2,
                "labels": [{"name": "status: ready"}, {"name": "chore"}],
            },  # mechanical/opener
            {"number": 3, "labels": [{"name": "needs-triage"}]},  # NOT ready -> excluded
            # risk:major here proves SOURCE-LABEL PROPAGATION: PR #100 closes this issue, and the
            # closer item must carry the risk label so adversarial review can see it.
            {"number": 9, "labels": [{"name": "status: ready"}, {"name": "risk:major"}]},
            {
                "number": 10,
                "labels": [{"name": "status: ready"}, {"name": "coverage"}],
            },  # testgen/opener
            {"number": 11, "labels": [{"name": "status: ready"}, {"name": "epic"}]},  # epic/opener
            {
                "number": 12,
                "labels": [{"name": "status: ready"}, {"name": "refactor"}],
            },  # codemod/opener
            {
                "number": 13,
                "labels": [{"name": "status: ready"}, {"name": "runtime-ac"}],
            },  # runtime_ac/opener
            {"number": 14, "labels": [{"name": "status: ready"}]},  # non-agent PR ref -> excluded
            {"number": 15, "labels": [{"name": "status: ready"}]},  # meta:issue PR ref -> excluded
            {"number": 16, "labels": [{"name": "status: ready"}]},  # issue URL PR ref -> excluded
            {
                "number": 17,
                "labels": [{"name": "status: ready"}],
            },  # duplicate PR source -> excluded
        ],
        "o/B": [
            {"number": 5, "labels": [{"name": "status: ready"}, {"name": "dependencies"}]}
        ],  # mechanical
    }
    prs = {
        "o/A": [
            {
                "number": 100,
                "labels": [{"name": "agent:codex"}],
                "body": "PR body.",
                "closingIssuesReferences": [{"number": 9}],
            },  # closer; closes #9
            {
                "number": 101,
                "labels": [{"name": "bug"}],
                "closingIssuesReferences": [],
            },  # not agent -> excluded
            {
                "number": 102,
                "labels": [{"name": "bug"}],
                "closingIssuesReferences": [{"number": 14}],
            },
            {
                "number": 103,
                "labels": [{"name": "agent:codex"}],
                "body": "<!-- meta:issue:15 -->\nSource Issue #15",
                "closingIssuesReferences": [],
            },
            {
                "number": 104,
                "labels": [{"name": "agent:codex"}],
                "body": "Source: https://github.com/o/A/issues/16",
                "closingIssuesReferences": [],
            },
            {
                "number": 105,
                "labels": [{"name": "agent:codex"}],
                "headRefName": "orchestrator/issue-17-retry",
                "isDraft": True,
                "closingIssuesReferences": [],
            },
            {
                "number": 106,
                "labels": [{"name": "agent:codex"}],
                "headRefName": "orchestrator/issue-17",
                "isDraft": False,
                "closingIssuesReferences": [],
            },
            {
                "number": 107,
                "labels": [{"name": "agent:codex"}],
                "body": "<!-- meta:issue:18 -->",
                "closingIssuesReferences": [],
            },
        ],
        "o/B": [],
    }
    items = build_backlog(repos, lambda r: issues.get(r, []), lambda r: prs.get(r, []))
    # THE ARTIFACT MUST CARRY ITS OWN SCOPE, asserted against the REAL payload. The first version of
    # this grepped the source for the label -- and the assertion's own string literal satisfied the
    # grep, so it passed against the label's removal. Assert behaviour, never a mechanism vs itself.
    _pay = build_payload(items, set(), [], [])
    assert "NOT fleet workload" in _pay["scope"], _pay.get("scope")
    assert "REPO_REVIEW_PROCESS" in _pay["scope"], _pay.get("scope")
    by_t = {i["target"]: i for i in items}

    assert by_t["o/A#1"] == {
        "target": "o/A#1",
        "task_type": "implement",
        "lane": "opener",
        "labels": ["status: ready"],
        "title": "",
        "body": "Do thing.",
    }, by_t.get("o/A#1")
    assert by_t["o/A#2"]["body"] == "", by_t.get("o/A#2")
    assert by_t["o/A#2"]["task_type"] == "mechanical", by_t.get("o/A#2")  # chore -> mechanical
    assert by_t["o/A#10"]["task_type"] == "testgen", by_t.get("o/A#10")  # coverage -> testgen
    assert by_t["o/A#11"]["task_type"] == "epic", by_t.get("o/A#11")  # epic -> epic
    assert by_t["o/A#12"]["task_type"] == "codemod", by_t.get("o/A#12")  # refactor -> codemod
    assert by_t["o/A#13"]["task_type"] == "runtime_ac", by_t.get(
        "o/A#13"
    )  # runtime-ac -> runtime_ac
    assert by_t["o/B#5"]["task_type"] == "mechanical"  # dependencies -> mechanical
    assert "o/A#3" not in by_t, "non-ready issue must be excluded"
    assert "o/A#9" not in by_t, "issue referenced by an open PR must be excluded (no redo)"
    assert "o/A#14" not in by_t, "issue referenced by non-agent open PR must be excluded (no redo)"
    assert "o/A#15" not in by_t, "issue referenced by meta:issue open PR must be excluded (no redo)"
    assert "o/A#16" not in by_t, "issue referenced by issue URL open PR must be excluded (no redo)"
    assert (
        "o/A#17" not in by_t
    ), "issue referenced by source-issue branch PR must be excluded (no redo)"
    assert by_t["o/A#100"] == {
        "target": "o/A#100",
        "task_type": "implement",
        "lane": "closer",
        "labels": ["agent:codex"],
        "source_labels": ["risk:major", "status: ready"],
        "title": "",
        "body": "PR body.",
    }
    # END-TO-END: risk metadata lives on the ISSUE, the closer lane reads PR labels, and no PR in
    # the fleet carries a `risk:*` label. Without propagation the adversarial panel could never see
    # the one signal it acts on, which is why Travel-Plan-Permission#1429/#1436 merged unreviewed.
    import adversarial as _adv

    assert (
        _adv.high_stakes_reason(by_t["o/A#100"]) == "high-stakes label: risk:major"
    ), _adv.high_stakes_reason(by_t["o/A#100"])
    # The PR's OWN labels are untouched, so classify()/_has_agent_label see exactly what they did.
    assert by_t["o/A#100"]["labels"] == ["agent:codex"]
    # A closer whose source issue carries no risk label must NOT trip the panel.
    assert _adv.high_stakes_reason(by_t["o/A#103"]) is None, by_t["o/A#103"]
    assert "o/A#101" not in by_t, "non-agent PR must be excluded"
    assert "o/A#102" not in by_t, "non-agent PR must not become a closer"
    assert by_t["o/A#103"]["lane"] == "closer", by_t.get("o/A#103")
    assert by_t["o/A#104"]["lane"] == "closer", by_t.get("o/A#104")
    assert "o/A#105" not in by_t, "draft duplicate source-issue PR must be suppressed"
    assert by_t["o/A#106"]["lane"] == "closer", by_t.get("o/A#106")
    assert "o/A#107" not in by_t, "PR whose source issue is no longer open must be suppressed"

    # scoped-blocker exclusion
    items2 = build_backlog(
        repos, lambda r: issues.get(r, []), lambda r: prs.get(r, []), scoped={"o/A#1", "o/A#100"}
    )
    t2 = {i["target"] for i in items2}
    assert "o/A#1" not in t2 and "o/A#100" not in t2, "scoped-blocked targets must be excluded"
    assert "o/B#5" in t2

    # classify unit
    assert classify(["bug", "documentation"]) == "mechanical"
    assert classify(["enhancement", "tests"]) == "testgen"
    assert classify(["planning", "tests"]) == "epic"
    assert classify(["type: roadmap"]) == "epic"
    assert classify(["refactor", "tests"]) == "codemod"
    assert classify(["type: bulk-change"]) == "codemod"
    assert classify(["cross-repo", "tests"]) == "cross_repo"
    assert classify(["type: sync-manifest"]) == "cross_repo"
    assert classify(["runtime-ac"]) == "runtime_ac"
    assert classify(["type: verification-spec"]) == "runtime_ac"
    assert classify(["tests", "acceptance-criteria"]) == "runtime_ac"
    assert classify(["enhancement"]) == "implement"

    # --- scoped-blocker EXPIRY is honoured (root-cause fix 2026-08-10) --------------------------
    import tempfile

    global SENTINEL
    _saved_sentinel = SENTINEL
    with tempfile.TemporaryDirectory(prefix="backlog-blocker-selftest-") as td:
        SENTINEL = Path(td) / "lane-handoff.json"
        now = 1_800_000_000
        SENTINEL.write_text(
            json.dumps(
                {
                    "stop": {
                        "scoped_blockers": {
                            "o/r#1": {
                                "reason": "lapsed human wait",
                                "await_human": True,
                                "expires_at": "2026-01-01T00:00:00Z",
                            },  # long past
                            "o/r#2": {
                                "reason": "still current",
                                "await_human": True,
                                "expires_at": "2099-01-01T00:00:00Z",
                            },  # far future
                            "o/r#3": {
                                "reason": "no expiry -> unbounded block is deliberate",
                                "await_human": True,
                            },  # absent
                            "o/r#4": {
                                "reason": "machine reason",
                                "await_human": False,
                                "expires_at": "2026-01-01T00:00:00Z",
                            },  # expired, not human-awaited
                        }
                    }
                }
            )
        )
        live = load_scoped_blockers(now=now)
        assert live == {"o/r#2", "o/r#3"}, live  # expired ones no longer block; no-expiry does
        expired = expired_scoped_blockers(now=now)
        assert set(expired) == {"o/r#1", "o/r#4"}, expired
        # A blocker whose expiry cannot be parsed must keep blocking (fail-safe).
        SENTINEL.write_text(
            json.dumps(
                {
                    "stop": {
                        "scoped_blockers": {
                            "o/r#9": {
                                "reason": "garbled",
                                "await_human": True,
                                "expires_at": "not-a-date",
                            }
                        }
                    }
                }
            )
        )
        assert load_scoped_blockers(now=now) == {"o/r#9"}, "unparseable expiry must still block"
        assert expired_scoped_blockers(now=now) == {}, "unparseable expiry is not 'expired'"
    SENTINEL = _saved_sentinel

    print(
        "backlog.py selftest: OK (ready-issue + in-flight-agent-PR discovery, "
        "body retention, label classification, open-PR-referenced-issue exclusion, "
        "scoped-blocker filter + expiry honoured/fail-safe)"
    )


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        _selftest()
        return 0
    if "--live" not in argv and "--dry-run" not in argv:
        print("usage: backlog.py --live | --dry-run | --selftest", file=sys.stderr)
        return 2
    live_blockers = load_scoped_blockers()
    expired = expired_scoped_blockers()
    items = build_backlog(SUPPORTED_REPOS, live_fetch_issues, live_fetch_prs, scoped=live_blockers)
    # A lapsed human-awaited block silently starts letting work through. Raise it to the owner on
    # the live path only (dry-run stays read-only), deduped per target by the owner-question store.
    raised = raise_expired_blocker_questions() if "--live" in argv else []
    payload = build_payload(items, live_blockers, expired, raised)
    if "--live" in argv:
        HANDOFF.mkdir(parents=True, exist_ok=True)
        BACKLOG_JSON.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
