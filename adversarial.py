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

import json
import re
import shlex
import sys
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
        if re.search(pattern, title, flags=re.I):
            _capability_heartbeat("match")
            return f"high-stakes title match: {pattern}"
    return None


def is_high_stakes(item: dict) -> bool:
    return high_stakes_reason(item) is not None


def reviewers_from_env(env: dict | None = None) -> list[str]:
    env = env or {}
    raw = env.get("ORCH_ADVERSARIAL_REVIEWERS", "")
    reviewers = [part.strip() for part in raw.split(",") if part.strip()]
    return reviewers or list(DEFAULT_REVIEWERS)


def review_enabled(env: dict | None = None) -> bool:
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
                f"at least {unexamined_min} of {int(findings_submitted)} submitted findings received no "
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
    # Credit at the function the driver actually calls. tick.py calls adversarial.review()
    # / high_stakes_reason(); the heartbeat sat only in main(), so the panel could run
    # without the capability ever being credited. (2026-08-20)
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


def _selftest():
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


def main(argv):
    _capability_heartbeat()
    if "--selftest" in argv:
        _selftest()
        return 0
    print("usage: adversarial.py --selftest  (review() is called by the orchestrator/scheduler)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
