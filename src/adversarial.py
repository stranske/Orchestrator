#!/usr/bin/env python3
"""adversarial.py — adversarial review as a first-class orchestrator feature.

Distinct from the advisory cross-eval (comparative scoring) and the routing review: here N reviewers are
prompted to REFUTE — find the fatal flaw, default to "blocked unless proven sound" — and a MINORITY-VETO
ensemble adjudicates. Grounded in the LLM-judge bias literature: agreeableness bias makes single judges
rubber-stamp, and a few well-justified vetoes raise the true-negative rate (arxiv 2510.11822). Use for
"is this ACTUALLY correct / safe to merge", where being wrong is expensive — NOT for routine advisory review.

Adjudicate, don't obey: a veto is a flag to VERIFY against ground truth (tests, repo conventions), per the
lesson in ORCHESTRATOR.md — so review() returns the blockers for the orchestrator to weigh, not an order.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Mapping
from pathlib import Path

import dispatcher

# Reviewer auth/PATH handled by dispatcher.offload (read-only). Severity that counts as a veto.
VETO_SEVERITIES = {"high", "critical", "fatal"}
# One reviewer, chosen per run by the router (ability on the `review` route first, then capacity and
# load, with its exploration re-testing the alternatives), never the agent that wrote the change, and
# vibe only when no other agent is selectable. The panel key carries this token instead of a name,
# so a head is judged once whichever agent took the seat.
AUTO_REVIEWER = "auto"
REVIEW_TASK_TYPE = "review"
LAST_RESORT_REVIEWERS = frozenset({"vibe"})
MAX_REVIEWER_ATTEMPTS = 3
HIGH_STAKES_LABELS = {
    # Fleet risk vocabulary. `risk:major` was ABSENT until 2026-08-20 while being the label the
    # fleet actually writes: `high_stakes_reason()` therefore returned None for every genuinely
    # high-stakes issue, including Travel-Plan-Permission#1429 ("Policy fails open: blocking rules
    # pass when inputs are absent") and #1436 ("Audit record and state change are not atomic").
    # The code accepted `risk:critical`/`risk:high`, which no repo in the fleet uses. Verified
    # against the live label index: the fleet spells severity risk:major / risk:medium / risk:minor
    # / risk:low, and only `major` is high-stakes.
    "risk:major",
    "risk:critical",
    "risk:high",
    "breaking change",
    "breaking-change",
    "critical",
    "data loss",
    "data-loss",
    "database",
    "db-migration",
    "high risk",
    "high stakes",
    "high-risk",
    "high-stakes",
    "migration",
    "schema",
    "security",
    "auth",
    "authentication",
    "authorization",
}
HIGH_STAKES_TITLE_PATTERNS = (
    r"\bhigh[- ]risk\b",
    r"\bhigh[- ]stakes\b",
    r"\bbreaking[- ]change\b",
    r"\bsecurity\b",
    r"\bdata[- ]loss\b",
    r"\bauth(entication|orization)?\b",
    r"\bdb[- ]migration\b",
    r"\bdatabase[- ]migration\b",
    r"\bschema[- ]migration\b",
)

# One definition shared by the population counter and (after shadow measurement) the advisor.
HIGH_STAKES_MIN_CHANGED_LINES = 500
HIGH_STAKES_PATH_CLASSES = {"workflows", "github-meta", "auth", "data"}
HIGH_STAKES_PATH_PARTS = {
    "auth",
    "authentication",
    "authorization",
    "security",
    "data",
    "database",
    "db",
    "migrations",
    "persistence",
    "storage",
    "schema",
}
SHAPE_SHADOW_WEEK_SECONDS = 7 * 86400
SHAPE_SHADOW_REQUIRED_WEEKS = 2
SHAPE_MEASUREMENT_REF = "switch_review.adversarial-shape"


def shape_rule_id() -> str:
    """A rule edit must earn its own weekly observations; old evidence cannot activate it."""
    rule = {
        "version": 1,
        "classes": sorted(HIGH_STAKES_PATH_CLASSES),
        "parts": sorted(HIGH_STAKES_PATH_PARTS),
        "lines": HIGH_STAKES_MIN_CHANGED_LINES,
        "docs_only_excluded": True,
    }
    return hashlib.sha256(json.dumps(rule, sort_keys=True).encode()).hexdigest()[:16]


def high_stakes_from_shape(pr_facts: dict) -> str | None:
    """Pure, heartbeat-free shape identification. None is no positive identification.

    Raw paths retain auth/data changes that fleet_shapes' top-three classes can hide.
    A docs-only change is routine even when large. Unknown facts are diagnosed separately.
    """
    import fleet_shapes

    paths = pr_facts.get("paths")
    paths = paths if isinstance(paths, list) else []
    classes = set(pr_facts.get("path_classes") or [])
    classes.update(fleet_shapes.path_class(str(p)) for p in paths)
    hit = sorted(classes & HIGH_STAKES_PATH_CLASSES)
    if hit:
        return f"high-stakes path class: {', '.join(hit)}"
    for path in paths:
        if fleet_shapes.path_class(str(path)) in {"docs", "tests", "bookkeeping"}:
            continue
        parts = set(re.split(r"[/._-]+", str(path).lower()))
        hits = sorted(parts & HIGH_STAKES_PATH_PARTS)
        if hits:
            return f"high-stakes auth/data path: {path} ({', '.join(hits)})"
    if classes == {"docs"}:
        return None
    adds, dels = pr_facts.get("additions"), pr_facts.get("deletions")
    if type(adds) is int and type(dels) is int and adds >= 0 and dels >= 0:
        if adds + dels >= HIGH_STAKES_MIN_CHANGED_LINES:
            return f"high-stakes size: {adds + dels} changed lines"
    return None


def shape_facts_complete(facts: dict) -> bool:
    paths = facts.get("paths")
    total = facts.get("changedFiles", facts.get("files_total"))
    return (
        isinstance(paths, list)
        and all(isinstance(p, str) and p for p in paths)
        and type(total) is int
        and total == len(paths)
        and all(type(facts.get(k)) is int and facts[k] >= 0 for k in ("additions", "deletions"))
    )


def high_stakes_title_reason(facts: dict) -> str | None:
    """Preserve the closer title signal without recording a review invocation."""
    title = str(facts.get("title") or "")
    for pattern in HIGH_STAKES_TITLE_PATTERNS:
        if re.search(pattern, title, flags=re.IGNORECASE):
            return f"high-stakes title match: {pattern}"
    return None


def high_stakes_label_reason(facts: dict) -> str | None:
    """The existing label vocabulary, without matching/recording a review invocation."""
    for label in _label_names(facts):
        normalized = label.strip().lower().replace("_", "-")
        if normalized in HIGH_STAKES_LABELS or normalized.replace("-", " ") in HIGH_STAKES_LABELS:
            return f"high-stakes label: {label}"
    return None


def shape_shadow_readiness(*, path: Path | None = None, now: int | None = None) -> tuple[bool, str]:
    """Two complete weekly populations, at least a week apart, in the EXISTING ledger.

    This is only an advisor precondition, never review/merge authority. Missing, old, partial,
    future or different-rule observations cannot mature the shape route.
    """
    import capabilities

    now = int(time.time()) if now is None else now
    try:
        cap = capabilities.load_declared(path or capabilities.REG).get("adversarial-review", {})
        stamps = sorted(
            {
                e["timestamp"]
                for e in cap.get("event_history", [])
                if e.get("type") == "match"
                and e.get("ref") == SHAPE_MEASUREMENT_REF
                and e.get("metadata", {}).get("rule") == shape_rule_id()
                and e.get("metadata", {}).get("status") == "ok"
                and type(e.get("timestamp")) is int
                and e["timestamp"] <= now
            }
        )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return False, f"weekly shape evidence unavailable: {exc}"
    if len(stamps) < SHAPE_SHADOW_REQUIRED_WEEKS:
        return False, f"weekly shape measurements {len(stamps)}/{SHAPE_SHADOW_REQUIRED_WEEKS}"
    if stamps[-1] - stamps[0] < (SHAPE_SHADOW_REQUIRED_WEEKS - 1) * SHAPE_SHADOW_WEEK_SECONDS:
        return False, "weekly shape measurements are less than a week apart"
    if now - stamps[-1] > SHAPE_SHADOW_WEEK_SECONDS + 86400:
        return False, "latest weekly shape measurement is stale"
    return True, "two complete weekly shape measurements at least a week apart"


def record_shape_measurement(section: dict, *, now: int, path: Path | None = None) -> bool:
    """Production weekly match evidence, not an invocation, outcome or usefulness verdict."""
    import capabilities

    if os.environ.get("ORCH_CAPABILITY_HEARTBEATS") != "1" or section.get("status") != "ok":
        return False
    counts = [section.get(k) for k in ("shape_candidates", "label_candidates", "population")]
    if (
        section.get("rule") != shape_rule_id()
        or any(type(n) is not int or n < 0 for n in counts)
        or any(section[k] > section["population"] for k in ("shape_candidates", "label_candidates"))
    ):
        return False
    return capabilities.heartbeat(
        "adversarial-review",
        "match",
        ref=SHAPE_MEASUREMENT_REF,
        metadata={
            k: section[k]
            for k in ("rule", "status", "shape_candidates", "label_candidates", "population")
        },
        timestamp=now,
        path=path or capabilities.REG,
        idempotency_key=f"adversarial-shape:{shape_rule_id()}:{now // SHAPE_SHADOW_WEEK_SECONDS}",
    )


def _label_names(item: dict) -> list[str]:
    """The item's own labels PLUS its source issue's labels.

    Risk metadata lives on issues, not PRs — no PR in the fleet carries a `risk:*` label — and this
    check is restricted to the closer lane, which reads PR labels. Without `source_labels` the
    high-stakes test could never see the one signal it exists to act on. `backlog.build_backlog`
    attaches that key to closer items; everything else keeps reading `labels` unchanged.
    """
    names = []
    for key in ("labels", "source_labels"):
        for label in item.get(key) or []:
            if isinstance(label, dict):
                names.append(str(label.get("name", "")))
            else:
                names.append(str(label))
    return names


def high_stakes_reason(item: dict) -> str | None:
    """Return why a backlog item needs adversarial review, or None for routine work.

    This deliberately only triggers for closer PRs with explicit high-risk metadata.
    The dispatch path can then surface or run a review without making routine PRs
    spend multiple reviewer seats.
    """
    if item.get("lane") != "closer":
        return None
    for label in _label_names(item):
        normalized = label.strip().lower().replace("_", "-")
        if normalized in HIGH_STAKES_LABELS or normalized.replace("-", " ") in HIGH_STAKES_LABELS:
            # Credit on a POSITIVE identification only. tick.py calls this for every closer item,
            # so crediting unconditionally would report the capability as busy on routine work.
            _capability_heartbeat("match")
            return f"high-stakes label: {label}"
    title = str(item.get("title") or "")
    for pattern in HIGH_STAKES_TITLE_PATTERNS:
        if re.search(pattern, title, flags=re.IGNORECASE):
            _capability_heartbeat("match")
            return f"high-stakes title match: {pattern}"
    return None


def is_high_stakes(item: dict) -> bool:
    return high_stakes_reason(item) is not None


def reviewers_from_env(env: Mapping[str, str] | None = None) -> list[str]:
    """An explicit `ORCH_ADVERSARIAL_REVIEWERS` list, or `[AUTO_REVIEWER]`: one reviewer the router
    chooses at run time (`choose_reviewer`). The fixed default (codex, vibe, gemini) ended
    2026-10-12: in the 33-PR concept test vibe vetoed 30 of 33, so a two-seat panel at threshold 2
    was gemini's verdict paid twice, and a fixed seat cannot fall back when that one provider is
    out of capacity."""
    env = env or {}
    raw = env.get("ORCH_ADVERSARIAL_REVIEWERS", "")
    reviewers = [part.strip() for part in raw.split(",") if part.strip()]
    return reviewers or [AUTO_REVIEWER]


def review_enabled(env: Mapping[str, str] | None = None) -> bool:
    """On by default since 2026-10-12 (owner decision after the concept test); `0` is the kill
    switch. Advisory either way: nothing the panel returns blocks a merge."""
    env = env or {}
    return str(env.get("ORCH_RUN_ADVERSARIAL_REVIEW", "1")).strip() != "0"


def refute_prompt(context: str) -> str:
    """Refute-mode: the reviewer's job is to find the fatal flaw, not to bless. Default to blocked."""
    return (
        "You are an ADVERSARIAL reviewer. Your job is to REFUTE this change — find the flaw that makes it "
        "unsafe to merge. Read the actual code in this worktree. Assume it is broken until proven sound; do "
        "NOT give the benefit of the doubt. Look for: incorrect logic, missed spec requirements, broken "
        "edge cases, security holes, data-loss risk, and anything that would fail in production.\n\n"
        f"CONTEXT: {context}\n\n"
        'Return STRICT JSON only: {"blocker": true|false, "severity": "none|low|medium|high|critical", '
        '"finding": "<the single most serious problem, with file:line if possible>", '
        '"confidence": 0.0-1.0}'
    )


def _first_json(text: str) -> dict | None:
    if not text:
        return None
    for s in (m.start() for m in re.finditer(r"\{", text)):
        depth = 0
        for i in range(s, len(text)):
            depth += 1 if text[i] == "{" else (-1 if text[i] == "}" else 0)
            if depth == 0:
                try:
                    o = json.loads(text[s : i + 1])
                    if isinstance(o, dict) and "blocker" in o:
                        return o
                except Exception:
                    pass
                break
    return None


# A shortfall is neither a pass nor a block: the panel was too small for the threshold to be
# reachable, so NO adjudication is supportable in either direction. See aggregate_veto's docstring.
INCONCLUSIVE = "INCONCLUSIVE"


def _threshold_reachable(n_reviewers: int, veto_threshold: int) -> bool:
    """Could `veto_threshold` vetoes have been cast AT ALL by the reviewers that actually returned?

    A named function rather than an inline comparison so the selftest can BREAK it and prove the
    shortfall branch is load-bearing rather than vacuous.
    """
    return n_reviewers >= veto_threshold


def _coverage_floor(
    findings_submitted: int | None, n_reviewers: int
) -> tuple[int | None, int | None]:
    """(claims a verdict COULD have settled, claims that provably got none). None means unknown.

    Both are floors, not estimates: `refute_prompt` asks for one problem and `_first_json` keeps one
    object, so a reviewer settles at most one claim. Named, like `_threshold_reachable`, so the
    selftest can break it and prove the coverage branch is load-bearing.
    """
    if findings_submitted is None:
        return None, None
    n = int(findings_submitted)
    return min(n, n_reviewers), max(0, n - n_reviewers)


def aggregate_veto(
    verdicts: list[dict],
    veto_threshold: int = 2,
    reviewers_requested: int | None = None,
    findings_submitted: int | None = None,
) -> dict:
    """Minority-veto: count SUBSTANTIATED blockers (blocker=true AND severity in VETO_SEVERITIES). If at
    least `veto_threshold` reviewers veto, the verdict is BLOCKED. Pure + testable — the heart of the feature.

    LATCHED GATE, fixed 2026-08-23. Observed live in a real audit run (experiment
    `advice:a6cc531b8010`; full provenance in this capability's ledger `notes`, since the evidence
    itself is instance-local and not committed): with `reviewers=["codex","vibe"]` and
    `veto_threshold=2`, vibe returned null and this function reported
    `{"verdict": "PASS", "n_reviewers": 1, "n_vetoes": 1}` while CARRYING a high-severity
    0.99-confidence blocker. Once the reviewer population shrinks below the threshold, the threshold
    is unreachable BY CONSTRUCTION — and the failure presented as PASS, i.e. as silence.
    `aggregate_veto([])` was the same bug at its worst: zero reviewers returning read as a clean
    pass. This module's own docstring says "Adjudicate, don't obey", which is exactly why the
    aggregate must refuse to say PASS when PASS is the only thing it could have said.

    The three latched-gate questions, answered (~/.claude/skills/latched-gate-check):
    1. WHAT DECREMENTS THIS? Reviewers returning a parseable verdict — re-run the panel, repair the
       reviewer that failed, or lower the threshold. Not "time passes", not "someone notices".
    2. CAN THAT RUN WHILE THE GATE IS CLOSED? Yes; nothing here forbids re-running reviewers, which
       makes this a mis-report rather than a deadlock. The harm was that PASS gave no reason to.
    3. DOES THE MEASURING WINDOW EQUAL THE DRAINING WINDOW? No, and that mismatch IS the defect: the
       threshold is chosen against the reviewers REQUESTED while the vetoes are counted over the
       reviewers that RETURNED. Both populations are now reported in the same place and the shortfall
       between them is named, instead of being silently absorbed into PASS.

    Fails toward motion, not silence: a shortfall returns INCONCLUSIVE and still carries whatever
    blockers were found. PASS now means what it says — the panel WAS large enough to block and
    declined to, which is the no-single-voice-tyranny property the minority-veto design is for.

    SECOND SHORTFALL AXIS, added 2026-08-23: FINDING coverage. The same audit run recorded a
    second limitation next to the reviewer one — five refutable claims went in and four received
    no verdict at all. That is structural, not a fluke: `refute_prompt` asks each reviewer for
    "the single most serious problem", and `_first_json` keeps the FIRST object carrying a
    `blocker` key, so a reviewer contributes AT MOST ONE verdict however many claims the context
    held. Verdicts are also unattributed — nothing maps a verdict back to the claim it judges.

    So a caller submitting N claims to R reviewers has at least `N - R` claims that provably
    received no verdict, and the old payload said nothing about it: the reviewer denominator was
    visible while the finding denominator stayed invisible. That made the fix above WORSE in one
    respect — reporting one shortfall makes silence about the other read as deliberate.

    `findings_submitted` closes it. Left None the behaviour is unchanged (unknown, not zero — the
    same rule as missing cost telemetry: absence must never read as "all covered"). Given a count,
    the payload reports `findings_adjudicated_max` and `findings_unexamined_min` — a rigorous
    floor, since one verdict can settle at most one claim — and incomplete coverage is
    INCONCLUSIVE for the same reason a short panel is: asserting PASS over five claims having
    examined at most one is a claim the evidence cannot support. A substantiated BLOCKED still
    wins, because a corroborated blocker is actionable no matter what else went unexamined.
    """
    valid = [v for v in verdicts if v]
    vetoes = [
        v
        for v in valid
        if v.get("blocker") and str(v.get("severity", "")).lower() in VETO_SEVERITIES
    ]
    # `requested` defaults to the list as passed, because review() records a None per reviewer that
    # failed to return parseable JSON — so len(verdicts) already counts the absentees. Never below
    # the number that returned; a caller understating it must not produce "returned 2 of 1".
    requested = max(
        len(verdicts) if reviewers_requested is None else int(reviewers_requested), len(valid)
    )
    reachable = _threshold_reachable(len(valid), veto_threshold)
    # A FLOOR on the claims nobody judged, not an estimate. None means unknown, never zero.
    covered_max, unexamined_min = _coverage_floor(findings_submitted, len(valid))
    if len(vetoes) >= veto_threshold:
        verdict = "BLOCKED"
    elif not reachable or (unexamined_min or 0) > 0:
        verdict = INCONCLUSIVE
    else:
        verdict = "PASS"
    # BLOCKING quantity and DRAINABLE quantity in ONE string, per the workspace runtime rule:
    # "1 veto / threshold 2" alone reads as a near-miss you should be patient about; appending
    # "reviewers returned 1 of 2" makes the shortfall unmissable at a glance.
    summary = (
        f"{len(vetoes)} veto{'' if len(vetoes) == 1 else 'es'} / threshold {veto_threshold}, "
        f"reviewers returned {len(valid)} of {requested}"
    )
    if findings_submitted is not None:
        summary += f", findings adjudicated at most {covered_max} of {int(findings_submitted)}"
    out = {
        "verdict": verdict,
        "n_reviewers": len(valid),
        "reviewers_requested": requested,
        "reviewers_missing": requested - len(valid),
        "n_vetoes": len(vetoes),
        "veto_threshold": veto_threshold,
        "threshold_reachable": reachable,
        "summary": summary,
        "blockers": [
            {
                "severity": v.get("severity"),
                "finding": v.get("finding"),
                "confidence": v.get("confidence"),
            }
            for v in vetoes
        ],
    }
    if findings_submitted is not None:
        out["findings_submitted"] = int(findings_submitted)
        out["findings_adjudicated_max"] = covered_max
        out["findings_unexamined_min"] = unexamined_min
        # The panel cannot say WHICH claim a verdict judged, so never let a reader infer it did.
        out["findings_attributed"] = False
    if verdict == INCONCLUSIVE:
        reasons = []
        if not reachable:
            reasons.append(
                f"only {len(valid)} of {requested} reviewers returned a verdict, so the veto threshold "
                f"of {veto_threshold} was unreachable"
            )
        if (unexamined_min or 0) > 0:
            reasons.append(
                f"at least {unexamined_min} of {int(findings_submitted or 0)} submitted findings received no "
                f"verdict (one verdict settles at most one finding, and verdicts are unattributed)"
            )
        out["inconclusive_reason"] = (
            "; ".join(reasons) + " — this is NOT a pass; re-run the missing coverage"
        )
    return out


def review(
    worktree: str,
    reviewers: list[str],
    context: str,
    veto_threshold: int = 2,
    timeout: int = 900,
    findings_submitted: int | None = None,
) -> dict:
    """Run N adversarial reviewers (read-only) over a worktree and adjudicate by minority-veto. Returns the
    aggregate + raw verdicts for the orchestrator to ADJUDICATE against ground truth (never auto-obey).

    ONE VERDICT PER REVIEWER. `refute_prompt` asks for "the single most serious problem" and
    `_first_json` keeps the first object carrying a `blocker` key, so packing several refutable
    claims into `context` does NOT get you one verdict each — it gets you one verdict, about
    whichever claim that reviewer judged worst, and nothing says which. When `context` holds more
    than one claim, pass `findings_submitted=<count>` so the aggregate can report the coverage
    floor instead of implying the whole set was examined."""
    # Credit at the function the driver actually calls: merge_guard calls high_stakes_reason()
    # and, through review_at_head(), this (tick.py did until 2026-10-05). The heartbeat sat only
    # in main(), so the panel could run without the capability ever being credited. (2026-08-20)
    _capability_heartbeat()
    prompt = refute_prompt(context)
    verdicts, raw = [], {}
    for r in reviewers:
        mode = "assess" if r == "codex" else "full"  # read-only where supported
        out = dispatcher.offload(
            r,
            prompt,
            cwd=worktree,
            mode=mode,
            timeout=timeout,
            write_boundary=_git_write_boundary(r, worktree),
        )
        v = _first_json(out.get("output", ""))
        raw[r] = v
        if v:
            verdicts.append(v)
    # reviewers_requested is the drainable population: without it the aggregate cannot tell a
    # 1-of-1 panel from a 1-of-3 panel, which is how the shortfall used to read as PASS.
    agg = aggregate_veto(
        verdicts,
        veto_threshold,
        reviewers_requested=len(reviewers),
        findings_submitted=findings_submitted,
    )
    agg["by_reviewer"] = raw
    return agg


# ONE VERDICT PER PR HEAD (2026-10-05). The panel judges a PR's code, and the code is its head
# commit, so a verdict is a fact about (target, head) and nothing else. Until this date the only
# automatic caller was the tick's pre-delegation hook: it would have re-reviewed the same head on
# every hourly tick, and the event it recorded did not say which head it had judged.
# `review_at_head` is the one entry for every seat — `merge_guard`'s terminal merge, and the closer
# lane through `adversarial.py review` — and it judges again only when the head moves.
#
# Latched-gate answers, because the memo gates spend:
# 1. What clears an entry? A push: the key is a hash over the head, so a new head has no entry, and
#    the same code earns the same judgment.
# 2. Can that run while the entry stands? Yes. And only CONCLUSIVE verdicts (PASS, BLOCKED) are
#    reused: an INCONCLUSIVE panel is a reviewer shortfall ("NOT a pass; re-run the missing
#    coverage"), and reusing it would forbid exactly that re-run until an unrelated push.
# 3. One window: `panel_key` is the single function the record and the lookup both call.
# 4. Drained, it prints `memo: none` and judges; a Brain it cannot read prints `memo: unknown` and
#    judges too. "No verdict at this head" and "could not look" are never the same sentinel.
PANEL_RULE = "panel@head:v1"
CONCLUSIVE_VERDICTS = frozenset({"PASS", "BLOCKED"})
PANEL_ARTIFACT_DIR = "adversarial-panels"
_HEAD_RE = re.compile(r"[0-9a-f]{40}")
_TARGET_RE = re.compile(r"[\w.-]+/[\w.-]+#[1-9]\d*")


def panel_key(target: str, head: str, reviewers: list[str]) -> str:
    """The identity of ONE judgment: this PR, at exactly this head, by this set of reviewers."""
    import feedback

    return feedback._completion_hash(
        {"rule": PANEL_RULE, "target": target, "head": head, "reviewers": sorted(reviewers)}
    )


def _panel_artifact_path(key: str) -> Path:
    state = Path(os.environ.get("ORCH_STATE_DIR", Path.home() / ".codex" / "orchestrator"))
    return state / PANEL_ARTIFACT_DIR / f"{key.split(':')[-1]}.json"


def recorded_verdict(target: str, head: str, reviewers: list[str]) -> dict:
    """The newest CONCLUSIVE verdict the Brain holds for exactly this key, three-valued: `found`
    (with the verdict and its findings when the artifact is intact), `none` (read, and nothing
    conclusive at this head), `unknown` (the Brain could not be read). Inconclusive judgments at
    this head are counted beside the answer and never returned as one."""
    import feedback

    key = panel_key(target, head, reviewers)
    try:
        with feedback._conn() as c:
            rows = c.execute(
                "SELECT event_id, payload_json, updated_ts FROM completion_events "
                "WHERE producer='adversarial' AND event_type='panel' "
                "ORDER BY updated_ts DESC, event_id DESC"
            ).fetchall()
    except Exception as exc:
        return {"state": "unknown", "key": key, "error": str(exc)[:300]}
    inconclusive = 0
    for event_id, payload_json, recorded_ts in rows:
        try:
            payload = json.loads(payload_json)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("adjudication_id") != key:
            continue
        verification = payload.get("verification") or {}
        verdict = str(verification.get("adjudicated_verdict") or "").upper()
        if verdict not in CONCLUSIVE_VERDICTS:
            inconclusive += 1
            continue
        found = {
            "state": "found",
            "key": key,
            "verdict": verdict,
            "event_id": event_id,
            "recorded_ts": recorded_ts,
            "inconclusive_at_head": inconclusive,
        }
        expected = next(
            (
                ref.get("content_hash")
                for ref in payload.get("artifact_refs") or []
                if isinstance(ref, dict) and ref.get("kind") == "panel_result"
            ),
            None,
        )
        found["result"] = _read_panel_artifact(key, expected)
        return found
    return {"state": "none", "key": key, "inconclusive_at_head": inconclusive}


def _read_panel_artifact(key: str, expected_hash: str | None) -> dict | None:
    """The findings behind a reused verdict, only when the file still hashes to what the Brain
    recorded. The Brain keeps verdicts and hashes, never prose, so the findings live beside it."""
    import feedback

    path = _panel_artifact_path(key)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not expected_hash or feedback._completion_hash(text) != expected_hash:
        return None
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    return doc.get("result") if isinstance(doc, dict) else None


def _write_panel_artifact(key: str, doc: dict) -> dict:
    import feedback

    path = _panel_artifact_path(key)
    text = json.dumps(doc, indent=1, sort_keys=True, default=str)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        return {"error": str(exc)[:300]}
    return {
        "artifact_id": f"{PANEL_ARTIFACT_DIR}/{path.stem[:24]}",
        "content_hash": feedback._completion_hash(text),
    }


def _record_panel(target: str, head: str, reviewers: list[str], key: str, result: dict) -> dict:
    """One Brain event per judgment, on the target's latest run for lineage (or a stable synthetic
    id when no run exists, as the runtime-AC gate does). Never raises: the panel was paid for."""
    import feedback

    verdict = str(result.get("verdict") or "").upper() or "UNKNOWN"
    try:
        artifact = _write_panel_artifact(
            key,
            {
                "rule": PANEL_RULE,
                "target": target,
                "head": head,
                "reviewers": list(reviewers),
                "recorded_at": int(time.time()),
                "result": result,
            },
        )
        refs: list[dict] = [{"artifact_id": f"{target}@{head}", "kind": "pr_head"}]
        if artifact.get("content_hash"):
            refs.append({**artifact, "kind": "panel_result"})
        run_id = feedback.latest_run_id_for_target(target) or (
            "adversarial-panel:" + hashlib.sha256(target.encode()).hexdigest()[:24]
        )
        result_hash = feedback._completion_hash(result)
        event = feedback.record_completion_event(
            run_id,
            event_type="panel",
            phase="verification",
            producer="adversarial",
            status=verdict.lower(),
            event_id="panel:" + hashlib.sha256(f"{key}|{time.time_ns()}".encode()).hexdigest()[:32],
            payload={
                "panel_ids": [f"adversarial:{reviewer}" for reviewer in reviewers],
                "adjudication_id": key,
                "result_hashes": [result_hash],
                "artifact_refs": refs,
                "verification": {
                    "adjudicated_verdict": verdict,
                    "verifier_ids": list(reviewers),
                    "result_hashes": {"panel": result_hash},
                },
            },
        )
        return {**event, "artifact": artifact}
    except Exception as exc:
        return {"recorded": False, "error": str(exc)[:300]}


def _git(worktree: str, *args: str):
    import subprocess

    return subprocess.run(
        ["git", "-C", worktree, *args], capture_output=True, text=True, check=False
    )


def _worktree_head(worktree: str) -> str:
    out = _git(worktree, "rev-parse", "HEAD")
    if out.returncode != 0:
        raise RuntimeError(f"git rev-parse HEAD failed in {worktree}: {out.stderr.strip()[:200]}")
    return out.stdout.strip()


def _worktree_state(worktree: str) -> dict:
    """HEAD and the tracked-file changes, the two things a reviewer must leave as it found them."""
    status = _git(worktree, "status", "--porcelain", "--untracked-files=no")
    return {
        "head": _worktree_head(worktree),
        "dirty": (
            status.stdout.strip()
            if status.returncode == 0
            else f"unreadable: {status.stderr[:200]}"
        ),
    }


def _restore_worktree(worktree: str, head: str) -> None:
    _git(worktree, "checkout", "-q", "--force", "--detach", head)
    _git(worktree, "checkout", "-q", "--", ".")


def _git_write_boundary(agent: str, worktree: str) -> dict | None:
    """Make the worktree's git metadata unwritable for a reviewer, where this machine can hold it.

    MEASURED 2026-10-12 (33-PR concept test): reviewers run write-capable in the tree they judge,
    and in 2 of 35 executions one changed it — an edit to audit.py that the next reviewer then
    vetoed, and a checkout of an older commit that the next reviewer judged instead of the head.
    `write_boundary_roots` always re-grants the run's own cwd, so the tree itself cannot be
    protected here; its git directories can, which stops a moved HEAD or a commit. File edits are
    caught by the after-check in `review_at_head`, which also covers a machine where no boundary
    can be enforced (inside a codex sandbox seatbelt cannot nest), so None here is never unsafe."""
    import adapters

    if agent == "codex" or adapters.write_boundary_unenforceable(agent):
        return None  # codex reviews in its own read-only sandbox (mode "assess")
    dirs = []
    for flag in ("--absolute-git-dir", "--git-common-dir"):
        out = _git(worktree, "rev-parse", flag)
        if out.returncode == 0 and out.stdout.strip():
            path = Path(out.stdout.strip())
            dirs.append(str(path if path.is_absolute() else Path(worktree) / path))
    return {"protect": list(dict.fromkeys(dirs)), "allow": []} if dirs else None


def _tree_changed(after: dict, head: str) -> bool:
    """A reviewer left the tree at another commit or with tracked changes: its verdict judged other
    code than the head it would be recorded under."""
    return after.get("head") != head or bool(after.get("dirty"))


def author_agents(labels: list) -> set[str]:
    """The agents that wrote the change, read from its `agent:<name>` labels. Status labels share
    the prefix (agent:auto, agent:retry, agent:needs-attention), so only routable names count."""
    import router

    known = {e["agent"] for spec in router.ROUTE_TABLE.values() for e in spec["agents"]}
    names = set()
    for label in labels or []:
        name = str(label.get("name", "") if isinstance(label, dict) else label).strip().lower()
        if name.startswith("agent:") and name[6:] in known:
            names.add(name[6:])
    return names


def choose_reviewer(
    exclude: set[str] | frozenset[str] = frozenset(),
    *,
    cap: dict | None = None,
    learned: dict | None = None,
    select_fn=None,
) -> dict:
    """ONE reviewer for the panel, by the router's own rule: ability on the `review` route (its
    learned ranks), then capacity and load, with its exploration re-testing the others. Never an
    agent in `exclude` (the change's authors, and any that already failed this run); vibe only when
    nothing else is selectable. Returns {agent, reason}; agent None when no seat is available."""
    import router

    route = [e["agent"] for e in router.ROUTE_TABLE[REVIEW_TASK_TYPE]["agents"]]
    pool = [a for a in route if a not in exclude]
    cap = router.load_capacity() if cap is None else cap
    if learned is None:
        learned = (router.learned_ranks() or {}).get(REVIEW_TASK_TYPE)
    select = select_fn or router.select_agent
    for tier, only in (
        ("primary", {a for a in pool if a not in LAST_RESORT_REVIEWERS}),
        ("last resort", {a for a in pool if a in LAST_RESORT_REVIEWERS}),
    ):
        if not only:
            continue
        picked = select(REVIEW_TASK_TYPE, cap, only=only, learned=learned)
        agent = picked.get("agent") if isinstance(picked, dict) else None
        if agent:
            return {
                "agent": agent,
                "reason": f"router {REVIEW_TASK_TYPE} route, {tier}; excluded "
                f"{', '.join(sorted(exclude)) or 'none'}",
            }
    # The router skips agents whose capacity is UNKNOWN as well as shed. A missing or stale snapshot
    # makes every agent unknown, and that must not leave every high-stakes merge unjudged: take the
    # best-ranked agent not known to be shed, vibe still last. If it cannot run, the caller's
    # attempts loop moves on to the next choice. (Found 2026-10-12 running the panel end to end.)
    ranks = learned or {}
    unshed = [a for a in pool if router._state(cap, a) != "shed"]
    ordered = sorted(
        unshed,
        key=lambda a: (
            a in LAST_RESORT_REVIEWERS,
            (ranks.get(a) or {}).get("rank", len(route)),
            route.index(a),
        ),
    )
    if ordered:
        return {
            "agent": ordered[0],
            "reason": f"router selected none (capacity unknown); learned {REVIEW_TASK_TYPE} order, "
            f"excluded {', '.join(sorted(exclude)) or 'none'}",
        }
    return {
        "agent": None,
        "reason": f"no {REVIEW_TASK_TYPE} agent outside {sorted(exclude)} that is not shed",
    }


def panel_worktree(target: str, head: str) -> str:
    """A detached checkout of exactly `head`, private to the panel. The closer's worktree tracks
    the PR branch by name (gone once a merge deletes it) and is the tree agents push from, so a
    reviewer never runs there; `refs/pull/N/head` outlives the branch."""
    import provision

    repo, num = provision.parse_target(target)
    canon = provision.ensure_canonical(repo)
    wt = provision.WORKTREES_DIR / f"panel-{provision.repo_slug(repo)}-{num}-{head[:12]}"
    if wt.exists():
        state = _worktree_state(str(wt))
        if state["head"] == head and not state["dirty"]:
            return str(wt)
        _git(str(canon), "worktree", "remove", "--force", str(wt))
    _git(str(canon), "fetch", "-q", "origin", f"refs/pull/{num}/head")
    added = _git(str(canon), "worktree", "add", "-q", "--detach", str(wt), head)
    if added.returncode != 0:
        raise RuntimeError(
            f"cannot check out {head[:12]} for {target}: {added.stderr.strip()[:300]}"
        )
    return str(wt)


def _shortfall(reason: str, attempts: list) -> dict:
    return {
        "verdict": INCONCLUSIVE,
        "n_reviewers": 0,
        "reviewers_requested": 1,
        "reviewers_missing": 1,
        "n_vetoes": 0,
        "veto_threshold": 1,
        "summary": f"0 vetoes / threshold 1, reviewers returned 0 of 1 ({reason})",
        "blockers": [],
        "inconclusive_reason": reason + " — this is NOT a pass; re-run the missing coverage",
        "attempts": attempts,
    }


def review_at_head(
    target: str,
    head: str,
    *,
    reviewers: list[str] | None = None,
    context: str = "",
    worktree: str | None = None,
    env: Mapping[str, str] | None = None,
    exclude: set[str] | frozenset[str] = frozenset(),
    lookup_fn=None,
    provision_fn=None,
    head_fn=None,
    state_fn=None,
    restore_fn=None,
    choose_fn=None,
    review_fn=None,
    record_fn=None,
) -> dict:
    """The panel on `target` at exactly `head`, judged once: a conclusive verdict recorded for this
    (target, head, reviewer mode) is reused (`status: reused`), and otherwise the panel runs on a
    worktree proven to sit at that head and its verdict is recorded (`status: executed`). A
    worktree at any other commit is refused (`head_mismatch`) and nothing is reviewed or recorded,
    so no head is ever credited with a judgment of another. Advisory like `review`: the caller
    adjudicates the blockers against ground truth.

    Default mode is ONE router-chosen reviewer (`choose_reviewer`) at threshold 1, outside
    `exclude` (the change's authors). A reviewer that returns no verdict, or leaves the tree
    changed, is replaced by the next choice, up to MAX_REVIEWER_ATTEMPTS, so one provider out of
    capacity does not leave the merge unjudged. The tree is checked AFTER every reviewer as well
    as before: a changed tree is restored and that verdict discarded, because it judged other code.
    """
    reviewers = list(reviewers) if reviewers else reviewers_from_env(env)
    auto = reviewers == [AUTO_REVIEWER]
    base: dict = {"target": target, "head": head, "reviewers": reviewers}
    if not _TARGET_RE.fullmatch(str(target or "")) or not _HEAD_RE.fullmatch(str(head or "")):
        return {
            **base,
            "status": "invalid",
            "detail": "target must be owner/repo#N and head a full 40-hex commit SHA",
        }
    lookup = (lookup_fn or recorded_verdict)(target, head, reviewers)
    # A count when the Brain answered (0 included), None when it could not be read.
    base.update(
        memo=lookup.get("state"),
        key=lookup.get("key"),
        inconclusive_at_head=lookup.get("inconclusive_at_head"),
    )
    if lookup.get("state") == "found":
        return {
            **base,
            "status": "reused",
            "verdict": lookup["verdict"],
            "recorded_ts": lookup.get("recorded_ts"),
            "event_id": lookup.get("event_id"),
            "result": lookup.get("result"),
        }
    if lookup.get("state") == "unknown":
        base["memo_error"] = lookup.get("error")
    try:
        if worktree is None:
            worktree = str((provision_fn or panel_worktree)(target, head))
        observed = (head_fn or _worktree_head)(str(worktree))
    except Exception as exc:
        return {**base, "status": "failed", "error": str(exc)[:500]}
    if observed != head:
        return {
            **base,
            "status": "head_mismatch",
            "observed_head": observed,
            "detail": "the worktree is not at the head this verdict would be recorded under; "
            "nothing was reviewed or recorded",
        }
    worktree = str(worktree)
    if state_fn is None and head_fn is not None:
        # a test seam that fakes the head fakes the whole tree: nothing on disk to read

        def state_fn(wt):
            return {"head": head_fn(wt), "dirty": ""}

    state_of = state_fn or _worktree_state
    restore = restore_fn or _restore_worktree
    seats: list[list[str] | None] = [None] * MAX_REVIEWER_ATTEMPTS if auto else [reviewers]
    tried: set[str] = set()
    attempts: list[dict] = []
    result: dict | None = None
    used: list[str] = []
    for fixed in seats:
        seat: list[str] = list(fixed or [])
        if auto:
            pick = (choose_fn or choose_reviewer)(set(exclude) | tried)
            if not pick.get("agent"):
                attempts.append({"agent": None, "outcome": "no_seat", "reason": pick.get("reason")})
                break
            seat = [pick["agent"]]
            tried.add(pick["agent"])
        try:
            # A plain call on the default path, so the audit's static call graph sees this entry
            # reach `review`'s heartbeat (`(review_fn or review)(...)` hides it).
            if review_fn is None:
                outcome = review(worktree, seat, context, veto_threshold=1 if auto else 2)
            else:
                outcome = review_fn(worktree, seat, context)
        except Exception as exc:
            return {**base, "status": "failed", "error": str(exc)[:500], "worktree": worktree}
        after = state_of(worktree)
        if _tree_changed(after, head):
            restore(worktree, head)
            attempts.append(
                {
                    "agent": ",".join(seat),
                    "outcome": "changed_tree",
                    "head_after": after.get("head"),
                    "dirty": str(after.get("dirty"))[:300],
                }
            )
            continue
        verdict = str(outcome.get("verdict") or "").upper()
        if auto and verdict not in CONCLUSIVE_VERDICTS:
            attempts.append({"agent": seat[0], "outcome": "no_verdict"})
            continue
        attempts.append({"agent": ",".join(seat), "outcome": verdict.lower()})
        result, used = outcome, seat
        break
    if result is None:
        result = _shortfall(
            "no reviewer returned a verdict on an unchanged tree "
            f"({len(attempts)} attempt(s): "
            + "; ".join(f"{a['agent']} {a['outcome']}" for a in attempts)
            + ")",
            attempts,
        )
        used = sorted(tried) or list(reviewers)
    else:
        result = {**result, "attempts": attempts}
    key = base.get("key") or panel_key(target, head, reviewers)
    verdict = str(result.get("verdict") or "").upper()
    return {
        **base,
        "status": "executed",
        "verdict": verdict,
        "conclusive": verdict in CONCLUSIVE_VERDICTS,
        "reviewed_by": used,
        "worktree": worktree,
        "result": result,
        "lineage": (record_fn or _record_panel)(target, head, used, key, result),
    }


# THE CONSUMER (owner decision 2026-10-12). Until this date nothing read a BLOCKED verdict: the
# value chain broke at "acted on" with 0 of 0 findings acted on. A BLOCKED panel now files ONE
# follow-up issue in the target repo for the fleet to fix or close, and the PR's source issue is
# closed at that time rather than left hanging (the owner's condition). Idempotent per panel key:
# a local record first, then a title search, so a re-run at the same head never files twice.
FOLLOWUP_TITLE = "Adversarial review follow-up: {target} @ {short}"
FOLLOWUP_PRIORITY = {
    "critical": "priority:high",
    "fatal": "priority:high",
    "high": "priority:normal",
}


def _followup_record_path(key: str) -> Path:
    return _panel_artifact_path(key).with_suffix(".followup.json")


def followup_body(target: str, head: str, outcome: dict) -> str:
    result = outcome.get("result") or {}
    blockers = result.get("blockers") or []
    reviewer = ", ".join(outcome.get("reviewed_by") or []) or "unrecorded"
    findings = (
        "\n".join(
            f"- **{b.get('severity')}** (confidence {b.get('confidence')}): {b.get('finding')}"
            for b in blockers
        )
        or "- (the verdict carried no finding text)"
    )
    repo, num = target.split("#")
    return (
        f"<!-- adversarial-panel:{outcome.get('key')} -->\n"
        "## Why\n\n"
        f"The adversarial review panel judged merged PR {repo}#{num} at head `{head}` and returned "
        f"BLOCKED (reviewer: {reviewer}). The merge was not held: the panel is advisory. Its finding "
        "is a claim to verify, not a confirmed defect.\n\n"
        f"{findings}\n\n"
        "## Scope\n\n"
        f"Only the finding above, in the code {repo}#{num} merged.\n\n"
        "## Non-Goals\n\n"
        "- Re-reviewing the rest of the PR.\n- Unrelated refactors.\n\n"
        "## Tasks\n\n"
        "- [ ] Reproduce the finding against current main (a failing test, or a cited code path).\n"
        "- [ ] If it reproduces: fix it, with a regression test that fails before the fix.\n"
        "- [ ] If it does not reproduce or is intended behaviour: close this issue as not planned, "
        "with the evidence.\n\n"
        "## Acceptance criteria\n\n"
        "- [ ] Either a merged fix with a regression test that fails on the pre-fix code, or a "
        "closing comment citing the code or test that shows the finding is not a defect.\n"
    )


def file_followup(target: str, head: str, outcome: dict, *, gh=None) -> dict:
    """File the follow-up for a BLOCKED panel and close the PR's still-open source issues.
    Never raises: the merge it follows has already happened."""
    import subprocess

    run = gh or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, check=False))
    if str(outcome.get("verdict") or "").upper() != "BLOCKED":
        return {"filed": False, "reason": f"verdict {outcome.get('verdict')}, nothing to follow up"}
    key = str(outcome.get("key") or panel_key(target, head, outcome.get("reviewers") or []))
    record = _followup_record_path(key)
    try:
        prior = json.loads(record.read_text(encoding="utf-8"))
        if prior.get("issue"):
            return {"filed": False, "reason": "already filed", **prior}
    except (OSError, ValueError):
        pass
    repo, num = target.split("#")
    title = FOLLOWUP_TITLE.format(target=target, short=head[:7])
    try:
        found = run(
            [
                "gh",
                "issue",
                "list",
                "-R",
                repo,
                "--state",
                "all",
                "--search",
                f'"{title}" in:title',
                "--json",
                "number,title",
            ]
        )
        existing = [i for i in json.loads(found.stdout or "[]") if i.get("title") == title]
        severities = [
            str(b.get("severity", "")).lower()
            for b in (outcome.get("result") or {}).get("blockers") or []
        ]
        wanted = next(
            (FOLLOWUP_PRIORITY[s] for s in ("critical", "fatal", "high") if s in severities), None
        )
        labels_out = run(
            ["gh", "label", "list", "-R", repo, "--search", "priority", "--json", "name"]
        )
        have = {row.get("name") for row in json.loads(labels_out.stdout or "[]")}
        label = wanted if wanted in have else None
        if existing:
            issue = existing[0]["number"]
        else:
            cmd = [
                "gh",
                "issue",
                "create",
                "-R",
                repo,
                "--title",
                title,
                "--body",
                followup_body(target, head, {**outcome, "key": key}),
            ]
            if label:
                cmd += ["--label", label]
            made = run(cmd)
            if made.returncode != 0:
                return {"filed": False, "error": (made.stderr or "")[:300]}
            issue = int(str(made.stdout).strip().rstrip("/").rsplit("/", 1)[-1])
        closed = []
        pr = run(["gh", "pr", "view", num, "-R", repo, "--json", "closingIssuesReferences,state"])
        for ref in json.loads(pr.stdout or "{}").get("closingIssuesReferences") or []:
            src = f"{ref['repository']['owner']['login']}/{ref['repository']['name']}"
            state = run(["gh", "issue", "view", str(ref["number"]), "-R", src, "--json", "state"])
            if json.loads(state.stdout or "{}").get("state") == "OPEN":
                done = run(
                    [
                        "gh",
                        "issue",
                        "close",
                        str(ref["number"]),
                        "-R",
                        src,
                        "--comment",
                        f"Delivered by {target}. The adversarial panel's open finding on that "
                        f"merge continues in {repo}#{issue}.",
                    ]
                )
                if done.returncode == 0:
                    closed.append(f"{src}#{ref['number']}")
    except Exception as exc:
        return {"filed": False, "error": str(exc)[:300]}
    out = {"issue": issue, "repo": repo, "label": label, "closed_source": closed, "key": key}
    try:
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps(out), encoding="utf-8")
    except OSError:
        pass
    return {"filed": not existing, **out}


def spawn_post_merge_panel(
    target: str, head: str, context: str, exclude: set[str], *, popen=None
) -> dict:
    """Run the panel and its follow-up DETACHED, after the merge. Advisory work never holds a merge
    or a lane round: a ~13-minute panel (median 806 s in the concept test) inside merge_guard would
    stall every high-stakes merge, and a lane that timed out mid-panel would block it de facto."""
    import subprocess

    log = _panel_artifact_path(panel_key(target, head, [AUTO_REVIEWER])).with_suffix(".log")
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "review",
        "--target",
        target,
        "--head",
        head,
        "--context",
        context,
        "--followup",
    ]
    if exclude:
        cmd += ["--exclude-agents", ",".join(sorted(exclude))]
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "ab") as fh:
            proc = (popen or subprocess.Popen)(
                cmd,
                stdout=fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
    except Exception as exc:
        return {"status": "spawn_failed", "error": str(exc)[:300]}
    return {"status": "scheduled_after_merge", "pid": getattr(proc, "pid", None), "log": str(log)}


def _selftest_review_at_head() -> None:
    """One verdict per (target, head), offline, against a disposable Brain and state dir."""
    import tempfile

    import feedback

    target, head, head2 = "o/r#7", "a" * 40, "b" * 40
    calls: list[str] = []

    def panel(verdict: str):
        def run(worktree, reviewers, context):
            calls.append(worktree)
            vetoes = [{"severity": "high", "finding": "auth bypass", "confidence": 0.9}]
            return {"verdict": verdict, "blockers": vetoes if verdict == "BLOCKED" else []}

        return run

    saved_db, saved_state = feedback.DB_PATH, os.environ.get("ORCH_STATE_DIR")
    with tempfile.TemporaryDirectory(prefix="adversarial-memo-") as tmp:
        feedback.DB_PATH = Path(tmp) / "brain.db"
        os.environ["ORCH_STATE_DIR"] = tmp
        try:

            def at(sha, verdict="BLOCKED", **kw):
                return review_at_head(
                    target,
                    sha,
                    reviewers=["vibe", "gemini"],
                    worktree=f"/wt/{sha[:4]}",
                    head_fn=lambda wt: wt.endswith(sha[:4]) and sha or "",
                    review_fn=panel(verdict),
                    **kw,
                )

            first = at(head)
            assert first["status"] == "executed" and first["memo"] == "none", first
            assert first["inconclusive_at_head"] == 0 and first["lineage"].get("event_id"), first
            again = at(head)
            assert again["status"] == "reused" and again["verdict"] == "BLOCKED", again
            assert again["result"]["blockers"][0]["finding"] == "auth bypass", again
            assert len(calls) == 1, calls  # the same head is judged once
            moved = at(head2, verdict="PASS")
            assert moved["status"] == "executed" and moved["verdict"] == "PASS", moved
            assert len(calls) == 2, calls  # a new head is judged afresh, never shown the old one
            # INCONCLUSIVE is a shortfall, not a judgment: the next call re-runs it.
            head3 = "c" * 40
            assert at(head3, verdict="INCONCLUSIVE")["conclusive"] is False
            rerun = at(head3, verdict="PASS")
            assert rerun["status"] == "executed" and rerun["inconclusive_at_head"] == 1, rerun
            # A worktree at another commit is refused: nothing reviewed, nothing recorded.
            refused = review_at_head(
                target,
                "d" * 40,
                reviewers=["vibe", "gemini"],
                worktree="/wt/other",
                head_fn=lambda wt: head,
                review_fn=panel("PASS"),
            )
            assert refused["status"] == "head_mismatch" and len(calls) == 4, refused
            assert recorded_verdict(target, "d" * 40, ["vibe", "gemini"])["state"] == "none"
            # A Brain that cannot be read is UNKNOWN, never "no verdict", and the panel still runs.
            blind = at(
                "e" * 40,
                lookup_fn=lambda *a: {"state": "unknown", "key": None, "error": "locked"},
            )
            assert blind["status"] == "executed" and blind["memo"] == "unknown", blind
            assert blind["inconclusive_at_head"] is None, blind
            # Findings are served only while the artifact still hashes to the recorded value.
            _panel_artifact_path(panel_key(target, head, ["vibe", "gemini"])).write_text("{}")
            tampered = at(head)
            assert tampered["status"] == "reused" and tampered["result"] is None, tampered
            assert review_at_head("o/r", head)["status"] == "invalid"
            assert review_at_head(target, "abc123")["status"] == "invalid"

            # DELIBERATE BREAK -> REVERT: a key that ignores the head shows a moved head the old
            # verdict, which is the defect this memo exists to prevent.
            saved_key = panel_key
            try:
                globals()["panel_key"] = lambda t, h, r: saved_key(t, head, r)
                broken = at("f" * 40, verdict="PASS")
                assert broken["status"] == "reused", "break did not change behaviour — vacuous"
            finally:
                globals()["panel_key"] = saved_key
            assert at("f" * 40, verdict="PASS")["status"] == "executed", "revert did not restore"
        finally:
            feedback.DB_PATH = saved_db
            if saved_state is None:
                os.environ.pop("ORCH_STATE_DIR", None)
            else:
                os.environ["ORCH_STATE_DIR"] = saved_state
    print(
        "adversarial.py review_at_head selftest: OK (one judgment per head, a moved head judged "
        "afresh, inconclusive re-run, head mismatch refused, unknown memo still runs, artifact "
        "hash checked, head-blind key break->revert)"
    )


def _selftest_one_reviewer() -> None:
    """One router-chosen reviewer, never the author, vibe last; a reviewer that fails or changes the
    tree is replaced; a BLOCKED verdict files one follow-up and closes the source issue. Offline:
    disposable Brain and state dir, stubbed router, git and gh — nothing is spawned."""
    import tempfile

    import feedback

    target, head = "o/r#7", "a" * 40
    # --- choose_reviewer: the author is never offered, vibe only when nothing else is selectable
    offered: list[set] = []

    def select(task, cap, *, only, learned):
        offered.append(set(only))
        live = sorted(only - {"cursor", "gemini", "codex", "claude"} if dead_primary else only)
        return {"agent": live[0]} if live else None

    dead_primary = False
    pick = choose_reviewer({"codex"}, cap={}, learned={}, select_fn=select)
    assert pick["agent"] and pick["agent"] != "codex" and pick["agent"] != "vibe", pick
    assert "codex" not in offered[0] and "vibe" not in offered[0], offered
    dead_primary = True
    last = choose_reviewer({"codex"}, cap={}, learned={}, select_fn=select)
    assert last["agent"] == "vibe" and "last resort" in last["reason"], last
    # capacity unknown (no snapshot): the router selects nothing, the learned order still seats one
    blind = choose_reviewer(
        {"codex"},
        cap={},
        learned={"claude": {"rank": 0}, "gemini": {"rank": 1}},
        select_fn=lambda *a, **k: None,
    )
    assert blind["agent"] == "claude" and "capacity unknown" in blind["reason"], blind
    everything_shed = {"agents": {a: {"state": "shed"} for a in ("cursor", "gemini", "claude")}}
    assert (
        choose_reviewer(
            {"vibe", "codex"}, cap=everything_shed, learned={}, select_fn=lambda *a, **k: None
        )["agent"]
        is None
    )
    assert author_agents(["agent:codex", "agent:auto", "agent:retry", "bug"]) == {"codex"}

    saved_db, saved_state = feedback.DB_PATH, os.environ.get("ORCH_STATE_DIR")
    with tempfile.TemporaryDirectory(prefix="adversarial-one-") as tmp:
        feedback.DB_PATH = Path(tmp) / "brain.db"
        os.environ["ORCH_STATE_DIR"] = tmp
        try:
            # --- a reviewer with no verdict, then one that MOVED HEAD, then a clean verdict
            queue = ["gemini", "cursor", "claude"]
            excluded: list[set] = []
            restored: list[str] = []
            seen: list[str] = []
            state: dict = {"n": 0, "sha": head}

            def choose(exclude):
                excluded.append(set(exclude))
                rest = [a for a in queue if a not in exclude]
                return {"agent": rest[0] if rest else None, "reason": "stub"}

            def review_fn(wt, seat, ctx):
                seen.append(seat[0])
                if seat[0] == "gemini":
                    return {"verdict": "INCONCLUSIVE"}
                return {
                    "verdict": "BLOCKED",
                    "blockers": [{"severity": "critical", "finding": "x"}],
                }

            def state_fn(wt):
                state["n"] += 1
                # the second reviewer (cursor) leaves the tree on an older commit
                return {"head": "f" * 40 if seen[-1] == "cursor" else state["sha"], "dirty": ""}

            def run(sha=head, **kw):
                state["sha"] = sha
                kw.setdefault("state_fn", state_fn)
                return review_at_head(
                    target,
                    sha,
                    reviewers=[AUTO_REVIEWER],
                    worktree="/wt",
                    exclude={"codex"},
                    head_fn=lambda wt: sha,
                    choose_fn=choose,
                    review_fn=review_fn,
                    restore_fn=lambda wt, h: restored.append(h),
                    **kw,
                )

            out = run()
            assert out["status"] == "executed" and out["verdict"] == "BLOCKED", out
            assert out["reviewed_by"] == ["claude"], out
            assert [a["outcome"] for a in out["result"]["attempts"]] == [
                "no_verdict",
                "changed_tree",
                "blocked",
            ], out["result"]["attempts"]
            assert restored == [head], restored  # the moved tree was put back
            assert all("codex" in e for e in excluded) and {"gemini", "cursor"} <= excluded[-1]
            # the verdict is keyed on the auto mode, so the next merge attempt reuses it
            assert run()["status"] == "reused"
            # --- no seat at all: a shortfall, recorded INCONCLUSIVE, re-run next time (not reused)
            queue.clear()
            none = run(sha="b" * 40)
            assert none["verdict"] == INCONCLUSIVE and none["conclusive"] is False, none
            assert run(sha="b" * 40)["status"] == "executed"

            # --- DELIBERATE BREAK -> REVERT: an after-check that sees no change records the
            # moved-tree verdict as the head's, the defect measured in the 2026-10-12 test.
            queue[:] = ["cursor", "claude"]
            seen.clear()
            saved_changed = _tree_changed
            try:
                globals()["_tree_changed"] = lambda after, h: False
                broken = run(sha="c" * 40)
                assert broken["reviewed_by"] == [
                    "cursor"
                ], "break did not change behaviour — vacuous"
            finally:
                globals()["_tree_changed"] = saved_changed
            seen.clear()
            fixed = run(sha="d" * 40)
            assert fixed["reviewed_by"] == ["claude"], "revert did not restore the after-check"

            # --- the consumer: one follow-up, the open source issue closed, idempotent
            calls: list[list[str]] = []

            def gh(cmd):
                import subprocess

                calls.append(cmd)
                verb = cmd[1:3]
                reply = {
                    ("issue", "list"): "[]",
                    ("label", "list"): json.dumps([{"name": "priority:high"}]),
                    ("issue", "create"): "https://github.com/o/r/issues/90\n",
                    ("pr", "view"): json.dumps(
                        {
                            "closingIssuesReferences": [
                                {"number": 40, "repository": {"name": "r", "owner": {"login": "o"}}}
                            ]
                        }
                    ),
                    ("issue", "view"): json.dumps({"state": "OPEN"}),
                    ("issue", "close"): "",
                }[tuple(verb)]
                return subprocess.CompletedProcess(cmd, 0, reply, "")

            filed = file_followup(target, head, out, gh=gh)
            assert (
                filed["filed"] and filed["issue"] == 90 and filed["label"] == "priority:high"
            ), filed
            assert filed["closed_source"] == ["o/r#40"], filed
            create = next(c for c in calls if c[1:3] == ["issue", "create"])
            assert "## Acceptance criteria" in create[create.index("--body") + 1], create
            n = len(calls)
            again = file_followup(target, head, out, gh=gh)
            assert again["filed"] is False and again["issue"] == 90 and len(calls) == n, again
            assert file_followup(target, "e" * 40, {"verdict": "PASS"}, gh=gh)["filed"] is False
            # --- the detached job carries --followup and the author exclusion; nothing really runs
            argv: list = []

            def fake_popen(cmd, **kw):
                argv.append(cmd)
                return type("P", (), {"pid": 1})()

            sched = spawn_post_merge_panel(
                target,
                head,
                "ctx",
                {"codex"},
                popen=fake_popen,
            )
            assert sched["status"] == "scheduled_after_merge" and "--followup" in argv[0], argv
            assert argv[0][argv[0].index("--exclude-agents") + 1] == "codex", argv
        finally:
            feedback.DB_PATH = saved_db
            if saved_state is None:
                os.environ.pop("ORCH_STATE_DIR", None)
            else:
                os.environ["ORCH_STATE_DIR"] = saved_state
    print(
        "adversarial.py one-reviewer selftest: OK (author excluded, vibe last resort, failed and "
        "tree-changing reviewers replaced, shortfall re-run, auto-mode memo, follow-up filed once "
        "with the source issue closed, detached job argv; after-check break->revert)"
    )


def _selftest():
    _selftest_review_at_head()
    _selftest_one_reviewer()
    p = refute_prompt("merge a payments change")
    assert "REFUTE" in p and "broken until proven sound" in p.lower() and '"blocker"' in p, p
    # minority-veto: 2 substantiated high vetoes meet threshold 2 -> BLOCKED
    vs = [
        {"blocker": True, "severity": "high", "finding": "off-by-one"},
        {"blocker": True, "severity": "critical", "finding": "auth bypass"},
        {"blocker": False, "severity": "none", "finding": ""},
    ]
    a = aggregate_veto(vs, veto_threshold=2)
    assert a["verdict"] == "BLOCKED" and a["n_vetoes"] == 2 and len(a["blockers"]) == 2, a
    # a lone low-severity concern does NOT block (no agreeableness-flip, but no single-voice tyranny either)
    a2 = aggregate_veto(
        [
            {"blocker": True, "severity": "low", "finding": "nit"},
            {"blocker": False, "severity": "none"},
        ],
        veto_threshold=2,
    )
    assert a2["verdict"] == "PASS" and a2["n_vetoes"] == 0, a2
    # SHORTFALL IS NOT A PASS. Regression pin for the live 2026-08-23 observation: one reviewer
    # returning of two requested cannot reach threshold 2, so the verdict must NOT be PASS.
    lone = [{"blocker": True, "severity": "high", "finding": "x", "confidence": 0.99}]
    short = aggregate_veto(lone, 2, reviewers_requested=2)
    assert short["verdict"] == INCONCLUSIVE, short
    assert short["n_reviewers"] == 1 and short["reviewers_requested"] == 2, short
    assert short["reviewers_missing"] == 1 and short["threshold_reachable"] is False, short
    # blocking quantity and drainable quantity in the same place, so a reader cannot mistake a
    # shortfall for a clean pass.
    assert short["summary"] == "1 veto / threshold 2, reviewers returned 1 of 2", short
    assert "NOT a pass" in short["inconclusive_reason"], short
    assert len(short["blockers"]) == 1, short  # the blocker is CARRIED, never swallowed
    # a None placeholder is a reviewer that was asked and did not answer -> counts as requested
    assert aggregate_veto(lone + [None], 2)["verdict"] == INCONCLUSIVE
    # total reviewer failure is the same bug at its worst: it used to read as a clean PASS
    empty = aggregate_veto([], 2, reviewers_requested=3)
    assert empty["verdict"] == INCONCLUSIVE and empty["reviewers_missing"] == 3, empty
    assert empty["summary"] == "0 vetoes / threshold 2, reviewers returned 0 of 3", empty
    # ...but a REACHABLE threshold the panel declines to meet is STILL a genuine PASS — no
    # single-voice tyranny. 3 of 3 returned, one high veto, threshold 2.
    minority = aggregate_veto(
        lone + [{"blocker": False, "severity": "none"}, {"blocker": False, "severity": "low"}], 2
    )
    assert minority["verdict"] == "PASS" and minority["threshold_reachable"] is True, minority
    assert minority["summary"] == "1 veto / threshold 2, reviewers returned 3 of 3", minority

    # ---- FINDING coverage, the second shortfall axis ------------------------------------------
    # Unknown must stay unknown: with findings_submitted omitted, nothing is asserted and the
    # payload is byte-identical to before, so existing callers are untouched.
    assert (
        "findings_submitted" not in minority and "findings_unexamined_min" not in minority
    ), minority
    assert minority["summary"].endswith("reviewers returned 3 of 3"), minority

    # 5 claims submitted, 3 reviewers returned -> at least 2 claims got NO verdict. Not a pass.
    five = aggregate_veto(
        lone + [{"blocker": False, "severity": "none"}, {"blocker": False, "severity": "low"}],
        2,
        findings_submitted=5,
    )
    assert five["verdict"] == INCONCLUSIVE, five
    assert five["findings_submitted"] == 5 and five["findings_adjudicated_max"] == 3, five
    assert five["findings_unexamined_min"] == 2, five
    assert five["findings_attributed"] is False, five
    assert five["summary"] == (
        "1 veto / threshold 2, reviewers returned 3 of 3, " "findings adjudicated at most 3 of 5"
    ), five
    assert "received no verdict" in five["inconclusive_reason"], five
    # The exact audit shape: 5 claims, 1 of 2 reviewers returned -> BOTH shortfalls named at once.
    both = aggregate_veto(lone, 2, reviewers_requested=2, findings_submitted=5)
    assert both["verdict"] == INCONCLUSIVE, both
    assert both["findings_unexamined_min"] == 4, both
    assert (
        "threshold" in both["inconclusive_reason"] and "no verdict" in both["inconclusive_reason"]
    ), both
    # Full coverage of a single claim by a big-enough panel is still a genuine PASS.
    one_of_one = aggregate_veto(
        [{"blocker": False, "severity": "none"}, {"blocker": False, "severity": "low"}],
        2,
        findings_submitted=1,
    )
    assert (
        one_of_one["verdict"] == "PASS" and one_of_one["findings_unexamined_min"] == 0
    ), one_of_one
    # A corroborated block still wins over incomplete coverage — a real blocker is actionable.
    blocked = aggregate_veto(
        [
            {"blocker": True, "severity": "high", "finding": "a"},
            {"blocker": True, "severity": "critical", "finding": "b"},
        ],
        2,
        findings_submitted=9,
    )
    assert blocked["verdict"] == "BLOCKED" and blocked["findings_unexamined_min"] == 7, blocked

    # DELIBERATE BREAK -> REVERT on the coverage floor: claim everything was examined and the
    # 5-claim/3-verdict case reverts to exactly the latched PASS this axis exists to stop.
    _panel = lone + [{"blocker": False, "severity": "none"}, {"blocker": False, "severity": "low"}]
    _saved_floor = _coverage_floor
    try:
        globals()["_coverage_floor"] = lambda submitted, n: (submitted, 0)
        broken_cov = aggregate_veto(_panel, 2, findings_submitted=5)
        assert broken_cov["verdict"] == "PASS", "break did not change behaviour — test is vacuous"
        assert broken_cov["findings_unexamined_min"] == 0, broken_cov
    finally:
        globals()["_coverage_floor"] = _saved_floor
    reverted = aggregate_veto(_panel, 2, findings_submitted=5)
    assert (
        reverted["verdict"] == INCONCLUSIVE and reverted["findings_unexamined_min"] == 2
    ), "revert did not restore the coverage floor"

    # DELIBERATE BREAK -> REVERT on the shortfall check, the correctness-critical half of the fix:
    # pretend the panel is always big enough and the 1-of-2 case reverts to the latched PASS.
    _saved_reachable = _threshold_reachable
    try:
        globals()["_threshold_reachable"] = lambda n, t: True
        broken = aggregate_veto(lone, 2, reviewers_requested=2)
        assert broken["verdict"] == "PASS", "break did not change behaviour — test is vacuous"
    finally:
        globals()["_threshold_reachable"] = _saved_reachable
    assert (
        aggregate_veto(lone, 2, reviewers_requested=2)["verdict"] == INCONCLUSIVE
    ), "revert did not restore the shortfall guard"
    assert (
        _first_json('noise {"blocker":true,"severity":"high","finding":"f"} tail')["severity"]
        == "high"
    )
    # high-stakes detection: closer-only and explicit risk metadata only.
    assert not is_high_stakes(
        {"lane": "opener", "labels": ["high-risk"], "title": "auth migration"}
    )
    assert not is_high_stakes({"lane": "closer", "labels": ["routine"], "title": "update copy"})
    assert is_high_stakes({"lane": "closer", "labels": ["risk:high"], "title": "update copy"})
    assert is_high_stakes({"lane": "closer", "labels": ["routine"], "title": "security fix"})
    assert reviewers_from_env({}) == [AUTO_REVIEWER]
    assert reviewers_from_env({"ORCH_ADVERSARIAL_REVIEWERS": ""}) == [AUTO_REVIEWER]
    assert reviewers_from_env({"ORCH_ADVERSARIAL_REVIEWERS": "vibe, gemini"}) == ["vibe", "gemini"]
    assert review_enabled({}) and review_enabled({"ORCH_RUN_ADVERSARIAL_REVIEW": "1"})
    assert not review_enabled({"ORCH_RUN_ADVERSARIAL_REVIEW": "0"})
    assert high_stakes_from_shape({"paths": [".github/workflows/gate.yml"]})
    assert high_stakes_from_shape({"paths": ["src/auth/session.py"]})
    assert (
        high_stakes_from_shape({"paths": ["docs/auth.md"], "additions": 1000, "deletions": 0})
        is None
    )
    assert high_stakes_label_reason({"labels": ["risk:major"]})

    print(
        "adversarial.py selftest: OK (refute prompt, minority-veto aggregation, reviewer-shortfall "
        "and finding-coverage guards each w/ break->revert, json extract, high-stakes "
        "detection, env helpers)"
    )


def _capability_heartbeat(event_type: str = "invocation") -> None:
    """Record that this capability ran, at its own code path.

    Infrastructure and lane capabilities are not always ROUTED to — they are entered directly — so
    each records use where it actually executes. Lazy import (capabilities imports feedback, and
    several of these are imported BY capabilities' dependencies), never raises (recording use must
    not be able to prevent the work), and inert outside an active tick via
    ORCH_CAPABILITY_HEARTBEATS. (2026-08-09)
    """
    try:
        import capabilities

        capabilities.production_heartbeat("adversarial-review", event_type, ref="adversarial.main")
    except Exception:
        pass


def _review_cli(argv: list[str]) -> int:
    """`adversarial.py review --target owner/repo#N --head <sha>`: the panel once per PR head.

    Exit 0 when a verdict is returned (reused or freshly judged), 2 when none could be (bad input,
    a worktree at another commit, a failed provision or run). The verdict itself is advisory and
    never sets the exit code."""
    import argparse

    parser = argparse.ArgumentParser(prog="adversarial.py review")
    parser.add_argument("--target", required=True, help="owner/repo#N")
    parser.add_argument("--head", required=True, help="the PR's full head commit SHA")
    parser.add_argument(
        "--reviewers",
        default="",
        help="comma-separated; default ORCH_ADVERSARIAL_REVIEWERS, else one router-chosen "
        "reviewer. A verdict is reused only for the same reviewer set (or the same auto mode)",
    )
    parser.add_argument(
        "--exclude-agents",
        default="",
        help="comma-separated agents that may not review (the change's authors)",
    )
    parser.add_argument(
        "--followup",
        action="store_true",
        help="after a BLOCKED verdict, file the follow-up issue and close the PR's open source issues",
    )
    parser.add_argument("--context", default="", help="what the reviewers are told about the PR")
    parser.add_argument("--worktree", help="a checkout already at --head (else one is provisioned)")
    parser.add_argument(
        "--lookup-only",
        action="store_true",
        help="report the verdict recorded for this head; never run the panel",
    )
    args = parser.parse_args(argv)
    reviewers = [part.strip() for part in args.reviewers.split(",") if part.strip()]
    reviewers = reviewers or reviewers_from_env(os.environ)
    if args.lookup_only:
        valid = _TARGET_RE.fullmatch(args.target) and _HEAD_RE.fullmatch(args.head)
        out = (
            recorded_verdict(args.target, args.head, reviewers)
            if valid
            else {"state": "invalid", "detail": "target owner/repo#N and a 40-hex head required"}
        )
        print(json.dumps(out, indent=2, default=str))
        return 0 if out.get("state") in {"found", "none"} else 2
    out = review_at_head(
        args.target,
        args.head,
        reviewers=reviewers,
        context=args.context or f"Pull request {args.target} at head {args.head[:12]}.",
        worktree=args.worktree,
        env=os.environ,
        exclude={a.strip() for a in args.exclude_agents.split(",") if a.strip()},
    )
    if args.followup and out.get("status") in {"reused", "executed"}:
        out["followup"] = file_followup(args.target, args.head, out)
    print(json.dumps(out, indent=2, default=str))
    return 0 if out.get("status") in {"reused", "executed"} else 2


def main(argv):
    _capability_heartbeat()
    if "--selftest" in argv:
        _selftest()
        return 0
    if argv and argv[0] == "review":
        return _review_cli(argv[1:])
    print(
        "usage: adversarial.py --selftest | adversarial.py review --target owner/repo#N "
        "--head <sha> [--reviewers a,b] [--context TEXT] [--worktree PATH] [--lookup-only]"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
