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
DEFAULT_REVIEWERS = ("codex", "vibe", "gemini")
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
    env = env or {}
    raw = env.get("ORCH_ADVERSARIAL_REVIEWERS", "")
    reviewers = [part.strip() for part in raw.split(",") if part.strip()]
    return reviewers or list(DEFAULT_REVIEWERS)


def review_enabled(env: Mapping[str, str] | None = None) -> bool:
    env = env or {}
    return env.get("ORCH_RUN_ADVERSARIAL_REVIEW") == "1"


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
        out = dispatcher.offload(r, prompt, cwd=worktree, mode=mode, timeout=timeout)
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


def _worktree_head(worktree: str) -> str:
    import subprocess

    out = subprocess.run(
        ["git", "-C", worktree, "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    if out.returncode != 0:
        raise RuntimeError(f"git rev-parse HEAD failed in {worktree}: {out.stderr.strip()[:200]}")
    return out.stdout.strip()


def review_at_head(
    target: str,
    head: str,
    *,
    reviewers: list[str] | None = None,
    context: str = "",
    worktree: str | None = None,
    env: Mapping[str, str] | None = None,
    lookup_fn=None,
    provision_fn=None,
    head_fn=None,
    review_fn=None,
    record_fn=None,
) -> dict:
    """The panel on `target` at exactly `head`, judged once: a conclusive verdict recorded for this
    (target, head, reviewers) is reused (`status: reused`), and otherwise the panel runs on a
    worktree proven to sit at that head and its verdict is recorded (`status: executed`). A
    worktree at any other commit is refused (`head_mismatch`) and nothing is reviewed or recorded,
    so no head is ever credited with a judgment of another. Advisory like `review`: the caller
    adjudicates the blockers against ground truth."""
    reviewers = list(reviewers) if reviewers else reviewers_from_env(env)
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
            if provision_fn is None:
                import provision

                provision_fn = provision.provision
            worktree = str(provision_fn(target, "closer"))
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
    try:
        # A plain call on the default path, so the audit's static call graph sees this entry
        # reach `review`'s heartbeat (`(review_fn or review)(...)` hides it).
        if review_fn is None:
            result = review(str(worktree), reviewers, context)
        else:
            result = review_fn(str(worktree), reviewers, context)
    except Exception as exc:
        return {**base, "status": "failed", "error": str(exc)[:500], "worktree": str(worktree)}
    key = base.get("key") or panel_key(target, head, reviewers)
    verdict = str(result.get("verdict") or "").upper()
    return {
        **base,
        "status": "executed",
        "verdict": verdict,
        "conclusive": verdict in CONCLUSIVE_VERDICTS,
        "worktree": str(worktree),
        "result": result,
        "lineage": (record_fn or _record_panel)(target, head, reviewers, key, result),
    }


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


def _selftest():
    _selftest_review_at_head()
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
    assert reviewers_from_env({}) == list(DEFAULT_REVIEWERS)
    assert reviewers_from_env({"ORCH_ADVERSARIAL_REVIEWERS": "vibe, gemini"}) == ["vibe", "gemini"]
    assert review_enabled({"ORCH_RUN_ADVERSARIAL_REVIEW": "1"})
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
        help="comma-separated; default ORCH_ADVERSARIAL_REVIEWERS, else codex,vibe,gemini. A "
        "verdict is reused only for the same reviewer set",
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
    )
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
