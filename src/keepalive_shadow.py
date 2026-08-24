#!/usr/bin/env python3
"""keepalive_shadow.py - shadow-mode corpus builder for keepalive PR supervision.

STAGE 2 of the docs/briefs/BRIEF_keepalive_transfers.md "#6 staged path": before any LIVE
supervisor is justified, accumulate evidence. For a keepalive-driven PR, this
module reads the keepalive state, reconstructs a `watch.py`-style report, asks the
existing advisory `redirect_policy.decide()` what it WOULD recommend, compares
that to what keepalive's blunt policy (detectStall / needs-human-after-N) WOULD
do, and records the pair to a local corpus.

IT TAKES NO LIVE ACTION. No kill, no decompose, no label, no merge. It only
records {keepalive_blunt, shadow_recommendation, disagreement, outcome}. This is
the deliberate guard against the split-brain / two-controller failure the
adversarial review flagged: there is exactly ONE controller (keepalive); this is
a passive observer building the A/B corpus that would later EARN a live
supervisor (see the review triggers in IMPROVEMENT_BACKLOG.md).

Signals come from the keepalive state marker embedded in a PR comment:
`<!-- keepalive-state:<version> {JSON} -->` (Workflows .github/scripts/keepalive_state.js).
The payload's aggregate counters (consecutive_zero_activity_rounds,
rounds_without_task_completion, iteration/max_iterations, failure, gate_conclusion,
last_files_changed) are the real per-PR signals.

Runtime: corpus lives on LOCAL disk (never the Dropbox checkout). Not wired into
cron yet beyond an opt-in step. `--selftest` runs fully offline.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import redirect_policy

# keepalive's current blunt thresholds (the baseline we measure against).
KEEPALIVE_STALL_THRESHOLD = 3  # consecutive zero-activity rounds before agent-switch
CHURN_THRESHOLD = 2  # rounds with commits but no task completion => churn
DEFAULT_FAILURE_THRESHOLD = 3  # needs-human after N failures (GoalsAndPlumbing §4)
CORPUS_PATH = Path(
    os.environ.get(
        "ORCH_KEEPALIVE_SHADOW_CORPUS",
        Path.home() / ".codex" / "orchestrator" / "keepalive-shadow" / "shadow.jsonl",
    )
)
READINESS_TARGET = 30  # labeled trajectories needed before an A/B is worth running

# Outcome taxonomy for the disagreement metric. The shadow A/B can only LEARN from FAILURE
# trajectories — where keepalive's blunt action led somewhere bad and a supervisor MIGHT have done
# better. A disagreement on a SUCCESS (merged/durable) is noise: keepalive's "continue" reached a good
# end, so the shadow's "inspect" never mattered. The provisional metric counted those successes and so
# overstated disagreement on merged-durable end-states; the refined metric weights FAILURES only.
FAILURE_OUTCOMES = {"needs_human", "reverted", "reopened", "closed_unmerged"}
SUCCESS_OUTCOMES = {"merged", "durable"}
PENDING_OUTCOMES = {"merged_pending"}


def _is_failure_outcome(outcome) -> bool:
    return outcome in FAILURE_OUTCOMES


RATE_LIMIT_LABELS = {"agent:rate-limited", "blocked-on-rate-reset"}
AUTH_LABELS = {"blocked-on-auth", "needs-human"}
# keepalive-state marker: `<!-- keepalive-state:<version> {JSON} -->` (keepalive_state.js:5,7)
STATE_REGEX = re.compile(r"<!--\s*keepalive-state(?::[\w.-]+)?\s+(.*?)\s*-->", re.DOTALL)


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def extract_state_payload(text: str) -> dict | None:
    """Return the LAST keepalive-state JSON payload embedded in PR comment text, or None."""
    payloads: list[dict] = []
    for raw in STATE_REGEX.findall(text or ""):
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                payloads.append(obj)
        except json.JSONDecodeError:
            continue
    return payloads[-1] if payloads else None


def normalize_signals(target: str, payload: dict | None, *, pr_state, labels) -> dict:
    """Map a raw keepalive-state payload + PR metadata to the normalized signal dict
    that synthesize_report/keepalive_blunt_action consume. Pure + offline-testable."""
    payload = payload or {}
    labels = list(labels or [])
    labels_lc = {str(label).strip().lower() for label in labels}
    state_lc = str(pr_state or "open").lower()
    if state_lc == "merged":
        outcome = "merged"
    elif state_lc == "closed":
        outcome = "closed_unmerged"
    elif labels_lc & {"needs-human", "agent:needs-attention"}:
        outcome = (
            "needs_human"  # keepalive gave up -> a labeled failure trajectory (visible while open)
        )
    else:
        outcome = None
    failure = payload.get("failure")
    failure_count = _int(failure.get("count")) if isinstance(failure, dict) else 0
    failure_count = max(failure_count, _int(payload.get("complete_gate_failure_rounds")))
    return {
        "target": target,
        "pr_state": state_lc,
        "labels": labels,
        "outcome": outcome,
        "iteration": _int(payload.get("iteration")),
        "max_iterations": _int(payload.get("max_iterations")),
        "consecutive_no_progress": _int(payload.get("consecutive_zero_activity_rounds")),
        "rounds_without_task_completion": _int(payload.get("rounds_without_task_completion")),
        "failure_count": failure_count,
        "failure_threshold": _int(payload.get("failure_threshold"), DEFAULT_FAILURE_THRESHOLD),
        "gate_conclusion": payload.get("gate_conclusion"),
        "last_has_changes": _int(payload.get("last_files_changed")) > 0,
        "total_tasks_completed": _int(payload.get("total_tasks_completed")),
        "has_marker": bool(payload),
    }


def keepalive_blunt_action(signals: dict) -> str:
    """Emulate keepalive's CURRENT blunt policy so we can record what it WOULD do.

    needs-human at >= failure_threshold failures; switch-agent at >= 3 consecutive
    zero-activity rounds; otherwise continue (the current loop does NOT hard-stop at
    max_iterations -- that hard cap is the cheap-win in Workflows #2480).
    """
    if signals.get("failure_count", 0) >= max(
        1, signals.get("failure_threshold", DEFAULT_FAILURE_THRESHOLD)
    ):
        return "needs-human"
    if signals.get("consecutive_no_progress", 0) >= KEEPALIVE_STALL_THRESHOLD:
        return "switch-agent"
    return "continue"


def synthesize_report(signals: dict) -> dict:
    """Map keepalive signals to a watch.py-style report for redirect_policy.decide().

    First increment: drift is inferred only as `broad_churn` when keepalive reports
    rounds-with-commits-but-no-task-completion (the churn case keepalive's
    effectiveness counter misses). Real semantic-drift (embedding the diff vs the
    acceptance criteria) is deferred.
    """
    labels = {str(label).strip().lower() for label in signals.get("labels") or []}
    hints = []
    if labels & RATE_LIMIT_LABELS:
        hints.append({"kind": "rate_limit"})
    if labels & AUTH_LABELS:
        hints.append({"kind": "auth"})

    state_raw = str(signals.get("pr_state") or "open").lower()
    last_changes = bool(signals.get("last_has_changes"))
    drift = {"severity": "none", "findings": []}

    if state_raw in ("closed", "merged"):
        state = "exited"
        recommended = "collect" if last_changes else "inspect"
    elif signals.get("consecutive_no_progress", 0) >= KEEPALIVE_STALL_THRESHOLD:
        state = "stalled"
        recommended = "inspect"
    elif signals.get("rounds_without_task_completion", 0) >= CHURN_THRESHOLD:
        state = "running"
        drift = {"severity": "medium", "findings": [{"kind": "broad_churn"}]}
        recommended = "wait"
    elif last_changes:
        state = "progress"
        recommended = "wait"
    else:
        state = "running"
        recommended = "wait"

    return {
        "state": state,
        "recommended_action": recommended,
        "signals": {"has_worktree_changes": last_changes},
        "hints": hints,
        "drift": drift,
        "errors": [],
    }


def _disagreement(blunt: str, shadow_action: str) -> bool:
    """RAW action divergence: do keepalive's blunt action and the shadow recommendation materially
    differ? (Outcome-independent.) Retained per-row so the A/B can redefine it; the A/B-relevant
    signal is `meaningful_disagreement` (this AND a FAILURE outcome). "Keep going both" agrees;
    one-intervenes-one-doesn't disagrees; blind switch-vs-targeted-recovery disagrees; premature
    human-vs-recoverable disagrees.
    """
    blunt_intervenes = blunt in ("switch-agent", "needs-human")
    shadow_intervenes = shadow_action in ("inspect", "redirect", "decompose")
    if blunt_intervenes != shadow_intervenes:
        return True
    if blunt == "switch-agent" and shadow_action in ("decompose", "redirect", "inspect"):
        return True
    if blunt == "needs-human" and shadow_action in ("redirect", "decompose"):
        return True
    return False


def meaningful_disagreement(blunt: str, shadow_action: str, outcome) -> bool:
    """The A/B-relevant disagreement: the actions diverge AND the trajectory actually FAILED.

    Disagreements on successful/pending/still-open trajectories are noise — keepalive reached a good
    (or not-yet-bad) end, so a divergent shadow recommendation would not have helped. Weighting only
    FAILURE outcomes is the refinement the corpus needs before any A/B is drawn from it
    (IMPROVEMENT_BACKLOG.md "Future development" staged-path #2)."""
    return _disagreement(blunt, shadow_action) and _is_failure_outcome(outcome)


def shadow_decide(signals: dict, history: list[dict] | None = None) -> dict:
    """Compute the shadow recommendation + the keepalive blunt baseline. No side effects."""
    report = synthesize_report(signals)
    decision = redirect_policy.decide(report, history or [])
    blunt = keepalive_blunt_action(signals)
    shadow_action = decision["action"]
    return {
        "target": signals.get("target"),
        "iteration": signals.get("iteration"),
        "max_iterations": signals.get("max_iterations"),
        "watch_state": report["state"],
        "keepalive_blunt": blunt,
        "shadow_action": shadow_action,
        "shadow_reason": decision["reason"],
        "disagreement": _disagreement(blunt, shadow_action),
        "drift": report["drift"]["severity"] != "none",
        "signals_summary": {
            "consecutive_no_progress": signals.get("consecutive_no_progress"),
            "rounds_without_task_completion": signals.get("rounds_without_task_completion"),
            "failure_count": signals.get("failure_count"),
            "gate_conclusion": signals.get("gate_conclusion"),
            "has_marker": signals.get("has_marker"),
        },
        "outcome": signals.get("outcome"),  # filled when known (merged/reverted/needs-human/...)
    }


def record(entry: dict, corpus_path: Path = CORPUS_PATH) -> dict:
    corpus_path.parent.mkdir(parents=True, exist_ok=True)
    with corpus_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
    return {"recorded": True, "corpus": str(corpus_path)}


def summarize(corpus_path: Path = CORPUS_PATH) -> dict:
    if not corpus_path.exists():
        return {"n": 0, "ready_for_ab": False, "readiness_target": READINESS_TARGET}
    rows = [
        json.loads(line)
        for line in corpus_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    n = len(rows)
    # Raw divergence (kept for transparency) vs the REFINED failure-weighted metric. Derived at READ
    # time from each row's stored actions + (possibly later-resolved) outcome, so it works across the
    # mixed-vintage append-only corpus with no migration.
    raw_disagreements = sum(1 for r in rows if r.get("disagreement"))
    failure_outcomes = sum(1 for r in rows if _is_failure_outcome(r.get("outcome")))
    meaningful_disagreements = sum(
        1
        for r in rows
        if meaningful_disagreement(
            r.get("keepalive_blunt", ""), r.get("shadow_action", ""), r.get("outcome")
        )
    )
    labeled = sum(1 for r in rows if r.get("outcome") and r["outcome"] not in PENDING_OUTCOMES)
    pending = sum(1 for r in rows if r.get("outcome") in PENDING_OUTCOMES)
    outcome_dist: dict[str, int] = {}
    for r in rows:
        oc = r.get("outcome")
        if oc:
            outcome_dist[oc] = outcome_dist.get(oc, 0) + 1
    blunt_dist: dict[str, int] = {}
    shadow_dist: dict[str, int] = {}
    for r in rows:
        blunt_dist[r.get("keepalive_blunt", "?")] = (
            blunt_dist.get(r.get("keepalive_blunt", "?"), 0) + 1
        )
        shadow_dist[r.get("shadow_action", "?")] = (
            shadow_dist.get(r.get("shadow_action", "?"), 0) + 1
        )
    return {
        "n": n,
        "labeled_outcomes": labeled,
        "pending_outcomes": pending,
        "failure_outcomes": failure_outcomes,
        "outcome_distribution": outcome_dist,
        "disagreements": raw_disagreements,
        "raw_disagreement_rate": round(raw_disagreements / n, 3) if n else 0.0,
        "meaningful_disagreements": meaningful_disagreements,
        # Headline = failure-weighted: of trajectories that FAILED, how often the shadow would have
        # diverged from keepalive's blunt action — the only A/B-relevant disagreement signal.
        "disagreement_rate": (
            round(meaningful_disagreements / failure_outcomes, 3) if failure_outcomes else 0.0
        ),
        "keepalive_blunt_distribution": blunt_dist,
        "shadow_action_distribution": shadow_dist,
        "ready_for_ab": labeled >= READINESS_TARGET,
        "readiness_target": READINESS_TARGET,
    }


def gather_signals(target: str, *, runner=subprocess.run) -> dict:
    """LIVE signal gather via gh: PR state/labels + the keepalive-state marker payload."""
    repo, _, num = target.partition("#")
    if not num:
        raise ValueError("target must be owner/repo#N")
    meta = runner(
        ["gh", "pr", "view", num, "-R", repo, "--json", "state,labels"],
        capture_output=True,
        text=True,
    )
    if meta.returncode != 0:
        raise RuntimeError(f"gh pr view failed: {(meta.stderr or '').strip()[-300:]}")
    mdoc = json.loads(meta.stdout or "{}")
    labels = [lab.get("name") for lab in (mdoc.get("labels") or []) if isinstance(lab, dict)]
    comments = runner(
        ["gh", "pr", "view", num, "-R", repo, "--json", "comments", "-q", ".comments[].body"],
        capture_output=True,
        text=True,
    )
    payload = extract_state_payload(comments.stdout if comments.returncode == 0 else "")
    return normalize_signals(target, payload, pr_state=mdoc.get("state"), labels=labels)


def _gh_throttle(resource: str) -> None:
    """Pace/defer against the shared GitHub rate budget (gh_capacity) when ORCH_GH_THROTTLE=1;
    no-op + fail-open otherwise so the backfill never breaks on a missing/erroring module."""
    try:
        import gh_capacity

        gh_capacity.throttle_if_enabled(resource)
    except Exception:
        pass


def _durability_outcome(
    target: str, *, runner=subprocess.run, revert_cache: dict | None = None
) -> str | None:
    """Durability label for a MERGED keepalive PR, reusing durability_sweep's classifier
    (revert/reopen detection via gh + 7-day grace). Returns durable|reverted|reopened|
    merged_pending, or None on error (caller falls back to coarse 'merged'). `revert_cache`
    is threaded so a backfill over many PRs does 1 revert search/repo, not 1/PR."""
    import durability_sweep

    repo, _, num = target.partition("#")
    res = runner(
        [
            "gh",
            "pr",
            "view",
            num,
            "-R",
            repo,
            "--json",
            "number,state,mergedAt,mergeCommit,baseRefName",
        ],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        return None
    pr = json.loads(res.stdout or "{}")
    pr["repo"] = repo
    verdict = durability_sweep.classify_durability(
        {"target": target, "mode": "remote"}, pr, revert_cache=revert_cache
    )
    return verdict.get("durability") or "merged_pending"


def _backfilled_targets(corpus_path: Path) -> set[str]:
    """Targets already recorded with source=backfill (so re-runs are idempotent)."""
    if not corpus_path.exists():
        return set()
    seen: set[str] = set()
    for line in corpus_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("source") == "backfill" and row.get("target"):
            seen.add(row["target"])
    return seen


def backfill(
    *,
    days: int = 90,
    grace_days: int = 7,
    limit: int = 150,
    since: str | None = None,
    until: str | None = None,
    runner=subprocess.run,
    durability: bool = True,
    durability_fn=None,
    corpus_path: Path = CORPUS_PATH,
) -> dict:
    """One labeled end-state entry per CLOSED keepalive PR updated in the last `days`.

    Historical seeding so the corpus can reach `ready_for_ab` without waiting weeks.
    NOTE: keepalive overwrites its state marker each round, so a closed PR retains only
    its FINAL state -> these are end-state snapshots, not full per-round trajectories.
    Merged PRs get the durability label (durable/reverted/reopened/merged_pending) unless
    durability=False (coarse 'merged'). Idempotent (skips already-backfilled targets).
    NO live action on any PR.
    """
    if since is None:
        since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    # exclude the durability grace window so merged PRs in the seed RESOLVE (not 'merged_pending')
    if until is None:
        until = (datetime.date.today() - datetime.timedelta(days=grace_days)).isoformat()
    _gh_throttle("search")  # `gh search prs` = SEARCH (30/min)
    res = runner(
        [
            "gh",
            "search",
            "prs",
            "--owner",
            "stranske",
            "--label",
            "agents:keepalive",
            "--state",
            "closed",
            "--updated",
            f"{since}..{until}",
            "--limit",
            str(limit),
            "--json",
            "repository,number",
            "--jq",
            '.[] | "\\(.repository.nameWithOwner)#\\(.number)"',
        ],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        return {
            "since": since,
            "error": (res.stderr or "gh search failed").strip()[-300:],
            "recorded": 0,
        }
    targets = [line.strip() for line in (res.stdout or "").splitlines() if line.strip()]
    already = _backfilled_targets(corpus_path)
    recorded = skipped = 0
    revert_cache: dict = {}  # repo -> cached revert search; 1 search/repo across the backfill
    for target in targets:
        if target in already:
            skipped += 1
            continue
        _gh_throttle("core")  # gather_signals + _durability_outcome do gh pr view (CORE)
        try:
            signals = gather_signals(target, runner=runner)
        except Exception:
            continue
        if signals.get("pr_state") == "merged" and durability:
            try:
                label = (durability_fn or _durability_outcome)(
                    target, runner=runner, revert_cache=revert_cache
                )
            except Exception:
                label = None
            signals = {**signals, "outcome": label or signals.get("outcome") or "merged"}
        entry = shadow_decide(signals)
        entry["source"] = "backfill"
        entry["backfill_since"] = since
        entry["backfill_until"] = until
        record(entry, corpus_path)
        recorded += 1
    return {
        "since": since,
        "until": until,
        "candidates": len(targets),
        "recorded": recorded,
        "skipped_already_backfilled": skipped,
        "capped": len(targets) >= limit,
    }


def _selftest() -> None:
    import tempfile

    # extract_state_payload pulls the JSON out of a real-shaped marker.
    marker_text = (
        'pre <!-- keepalive-state:v1 {"iteration":2,"max_iterations":5,'
        '"consecutive_zero_activity_rounds":1,"failure":{}} --> post'
    )
    p = extract_state_payload(marker_text)
    assert p and p["iteration"] == 2 and p["consecutive_zero_activity_rounds"] == 1, p
    assert extract_state_payload("no marker here") is None

    # normalize_signals reads the real payload field names + failure fallbacks.
    norm = normalize_signals(
        "o/r#9",
        {
            "failure": {"count": 2},
            "complete_gate_failure_rounds": 4,
            "consecutive_zero_activity_rounds": 3,
            "max_iterations": 5,
        },
        pr_state="OPEN",
        labels=["agent:codex"],
    )
    assert (
        norm["failure_count"] == 4 and norm["consecutive_no_progress"] == 3 and norm["has_marker"]
    ), norm

    def sig(target="o/r#1", pr_state="open", labels=None, **payload):
        return normalize_signals(target, payload, pr_state=pr_state, labels=labels or [])

    # A: 3 zero-activity rounds, no hint -> keepalive switches; shadow inspects -> disagree.
    a = shadow_decide(sig("o/r#1", consecutive_zero_activity_rounds=3))
    assert a["watch_state"] == "stalled" and a["keepalive_blunt"] == "switch-agent", a
    assert a["shadow_action"] == "inspect" and a["disagreement"] is True, a

    # B: stalled + rate-limited -> keepalive switches; shadow redirects -> disagree.
    b = shadow_decide(
        sig("o/r#2", labels=["agent:rate-limited"], consecutive_zero_activity_rounds=3)
    )
    assert b["keepalive_blunt"] == "switch-agent" and b["shadow_action"] == "redirect", b
    assert b["disagreement"] is True, b

    # C: healthy progress -> both keep going -> agree.
    c = shadow_decide(sig("o/r#3", last_files_changed=2))
    assert c["keepalive_blunt"] == "continue" and c["shadow_action"] == "wait", c
    assert c["disagreement"] is False and c["drift"] is False, c

    # D: churn (commits, zero task completion) -> keepalive "continue"; shadow flags
    # broad_churn drift -> inspect -> disagree. The case the cheap-win also targets.
    d = shadow_decide(sig("o/r#4", rounds_without_task_completion=2, last_files_changed=3))
    assert d["keepalive_blunt"] == "continue" and d["drift"] is True, d
    assert d["shadow_action"] == "inspect" and d["disagreement"] is True, d

    # E: merged exit with changes -> collect; outcome captured.
    e = shadow_decide(sig("o/r#5", pr_state="merged", last_files_changed=1))
    assert e["watch_state"] == "exited" and e["shadow_action"] == "collect", e
    assert e["outcome"] == "merged", e

    # needs-human label -> terminal-ish outcome captured even while the PR is still open.
    nh = shadow_decide(sig("o/r#8", labels=["needs-human"]))
    assert nh["outcome"] == "needs_human", nh

    # F: failure_count >= threshold -> keepalive needs-human; shadow says wait -> disagree.
    f = shadow_decide(sig("o/r#6", failure={"count": 3}, failure_threshold=3))
    assert f["keepalive_blunt"] == "needs-human" and f["shadow_action"] == "wait", f
    assert f["disagreement"] is True, f

    # No marker -> graceful: running/continue, has_marker False.
    g = shadow_decide(normalize_signals("o/r#7", None, pr_state="open", labels=[]))
    assert g["keepalive_blunt"] == "continue" and g["signals_summary"]["has_marker"] is False, g

    with tempfile.TemporaryDirectory(prefix="keepalive-shadow-") as tmp:
        corpus = Path(tmp) / "shadow.jsonl"
        for entry in (a, b, c, d):
            record(entry, corpus)
        record({**a, "outcome": "switch_succeeded"}, corpus)  # one labeled
        s = summarize(corpus)
        assert s["n"] == 5 and s["disagreements"] == 4, s
        assert s["labeled_outcomes"] == 1 and s["ready_for_ab"] is False, s
        assert s["keepalive_blunt_distribution"].get("switch-agent") == 3, s

    # Refined metric: weight ONLY disagreements on FAILURE outcomes; success-outcome disagreements
    # are the noise the provisional metric over-counted on merged-durable end-states.
    assert _is_failure_outcome("closed_unmerged") and not _is_failure_outcome("durable")
    assert meaningful_disagreement("switch-agent", "inspect", "needs_human") is True
    assert meaningful_disagreement("switch-agent", "inspect", "reverted") is True
    assert (
        meaningful_disagreement("switch-agent", "inspect", "durable") is False
    )  # success -> excluded
    assert meaningful_disagreement("switch-agent", "inspect", "merged_pending") is False
    assert meaningful_disagreement("continue", "wait", "needs_human") is False  # no raw divergence
    with tempfile.TemporaryDirectory(prefix="keepalive-shadow-fw-") as tmp:
        corpus = Path(tmp) / "shadow.jsonl"
        record({**a, "outcome": "needs_human"}, corpus)  # disagree + FAILURE -> meaningful
        record({**b, "outcome": "reverted"}, corpus)  # disagree + FAILURE -> meaningful
        record({**a, "outcome": "durable"}, corpus)  # disagree + SUCCESS -> noise (excluded)
        record(
            {**c, "outcome": "closed_unmerged"}, corpus
        )  # agree   + FAILURE -> not a disagreement
        s = summarize(corpus)
        assert s["disagreements"] == 3 and s["raw_disagreement_rate"] == round(3 / 4, 3), s
        assert s["failure_outcomes"] == 3, s  # needs_human, reverted, closed_unmerged
        assert s["meaningful_disagreements"] == 2, s  # only the disagreements on failures
        assert s["disagreement_rate"] == round(2 / 3, 3), s  # failure-weighted (raw would be 0.75)

    # backfill: historical seed from a fake gh; merged -> durability label; idempotent.
    search_commands = []

    def fake_runner(cmd, capture_output=True, text=True):
        if "search" in cmd:
            search_commands.append(list(cmd))
            out = "stranske/Repo#10\nstranske/Repo#11\n"
        elif "comments" in cmd:
            out = '<!-- keepalive-state:v1 {"rounds_without_task_completion":2,"last_files_changed":3} -->'
        elif "state,labels" in cmd:
            out = json.dumps({"state": "MERGED", "labels": [{"name": "agents:keepalive"}]})
        else:
            out = "{}"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    with tempfile.TemporaryDirectory(prefix="keepalive-shadow-bf-") as tmp:
        corpus = Path(tmp) / "shadow.jsonl"
        r1 = backfill(
            since="2026-01-01",
            runner=fake_runner,
            durability_fn=lambda t, **k: "durable",
            corpus_path=corpus,
        )
        assert r1["recorded"] == 2 and r1["candidates"] == 2, r1
        r2 = backfill(
            since="2026-01-01",
            runner=fake_runner,
            durability_fn=lambda t, **k: "durable",
            corpus_path=corpus,
        )
        assert r2["recorded"] == 0 and r2["skipped_already_backfilled"] == 2, r2  # idempotent
        r3 = backfill(
            since="2025-12-01",
            until="2025-12-31",
            runner=fake_runner,
            durability=False,
            corpus_path=Path(tmp) / "windowed.jsonl",
        )
        assert r3["since"] == "2025-12-01" and r3["until"] == "2025-12-31", r3
        assert any(
            "--updated" in cmd and "2025-12-01..2025-12-31" in cmd for cmd in search_commands
        ), search_commands
        bf = summarize(corpus)
        assert bf["n"] == 2 and bf["labeled_outcomes"] == 2, bf  # merged->durable labeled
        rows = [json.loads(line) for line in corpus.read_text().splitlines()]
        assert all(
            r["outcome"] == "durable" and r["source"] == "backfill" and r["backfill_until"]
            for r in rows
        ), rows

    print(
        "keepalive_shadow.py selftest: OK (marker extract, signal normalize, "
        "stall/rate-limit/progress/churn/exit/needs-human/no-marker synthesis, "
        "blunt-vs-shadow disagreement, failure-weighted disagreement metric, "
        "record/summarize, no live action)"
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Shadow-mode keepalive supervision corpus builder (no live action)."
    )
    parser.add_argument(
        "--shadow",
        metavar="owner/repo#N",
        help="gather a PR's signals, record the shadow vs blunt decision",
    )
    parser.add_argument(
        "--summarize", action="store_true", help="summarize the shadow corpus + A/B readiness"
    )
    parser.add_argument("--corpus", default=str(CORPUS_PATH))
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="historical seed: label closed keepalive PRs from the last --days",
    )
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument(
        "--since",
        default="",
        help="inclusive YYYY-MM-DD lower bound for --backfill; overrides --days",
    )
    parser.add_argument(
        "--until",
        default="",
        help="inclusive YYYY-MM-DD upper bound for --backfill; defaults to today minus --grace-days",
    )
    parser.add_argument(
        "--grace-days",
        type=int,
        default=7,
        help="exclude PRs updated within this many days (so merged ones resolve)",
    )
    parser.add_argument("--limit", type=int, default=150)
    parser.add_argument(
        "--no-durability",
        action="store_true",
        help="skip the durability label on merged PRs (faster, fewer gh calls)",
    )
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)

    if args.selftest:
        _selftest()
        return 0
    corpus = Path(args.corpus)
    if args.summarize:
        print(json.dumps(summarize(corpus), indent=2))
        return 0
    if args.backfill:
        print(
            json.dumps(
                backfill(
                    days=args.days,
                    grace_days=args.grace_days,
                    limit=args.limit,
                    since=args.since or None,
                    until=args.until or None,
                    durability=not args.no_durability,
                    corpus_path=corpus,
                ),
                indent=2,
            )
        )
        return 0
    if args.shadow:
        signals = gather_signals(args.shadow)
        decision = shadow_decide(signals)
        rec = record(decision, corpus)
        print(json.dumps({**decision, **rec}, indent=2))
        return 0
    parser.error("one of --shadow / --backfill / --summarize / --selftest is required")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
