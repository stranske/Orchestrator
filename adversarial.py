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
    "risk:major", "risk:critical", "risk:high",
    "breaking change", "breaking-change", "critical",
    "data loss", "data-loss", "database", "db-migration",
    "high risk", "high stakes", "high-risk", "high-stakes",
    "migration", "schema", "security",
    "auth", "authentication", "authorization",
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
                    o = json.loads(text[s:i + 1])
                    if isinstance(o, dict) and "blocker" in o:
                        return o
                except Exception:
                    pass
                break
    return None


def aggregate_veto(verdicts: list[dict], veto_threshold: int = 2) -> dict:
    """Minority-veto: count SUBSTANTIATED blockers (blocker=true AND severity in VETO_SEVERITIES). If at
    least `veto_threshold` reviewers veto, the verdict is BLOCKED. Pure + testable — the heart of the feature."""
    valid = [v for v in verdicts if v]
    vetoes = [v for v in valid if v.get("blocker") and str(v.get("severity", "")).lower() in VETO_SEVERITIES]
    verdict = "BLOCKED" if len(vetoes) >= veto_threshold else "PASS"
    return {"verdict": verdict, "n_reviewers": len(valid), "n_vetoes": len(vetoes),
            "veto_threshold": veto_threshold,
            "blockers": [{"severity": v.get("severity"), "finding": v.get("finding"),
                          "confidence": v.get("confidence")} for v in vetoes]}


def review(worktree: str, reviewers: list[str], context: str, veto_threshold: int = 2,
           timeout: int = 900) -> dict:
    """Run N adversarial reviewers (read-only) over a worktree and adjudicate by minority-veto. Returns the
    aggregate + raw verdicts for the orchestrator to ADJUDICATE against ground truth (never auto-obey)."""
    # Credit at the function the driver actually calls. tick.py calls adversarial.review()
    # / high_stakes_reason(); the heartbeat sat only in main(), so the panel could run
    # without the capability ever being credited. (2026-08-20)
    _capability_heartbeat()
    prompt = refute_prompt(context)
    verdicts, raw = [], {}
    for r in reviewers:
        mode = "assess" if r == "codex" else "full"      # read-only where supported
        out = dispatcher.offload(r, prompt, cwd=worktree, mode=mode, timeout=timeout)
        v = _first_json(out.get("output", ""))
        raw[r] = v
        if v:
            verdicts.append(v)
    agg = aggregate_veto(verdicts, veto_threshold)
    agg["by_reviewer"] = raw
    return agg


def _selftest():
    p = refute_prompt("merge a payments change")
    assert "REFUTE" in p and "broken until proven sound" in p.lower() and '"blocker"' in p, p
    # minority-veto: 2 substantiated high vetoes meet threshold 2 -> BLOCKED
    vs = [{"blocker": True, "severity": "high", "finding": "off-by-one"},
          {"blocker": True, "severity": "critical", "finding": "auth bypass"},
          {"blocker": False, "severity": "none", "finding": ""}]
    a = aggregate_veto(vs, veto_threshold=2)
    assert a["verdict"] == "BLOCKED" and a["n_vetoes"] == 2 and len(a["blockers"]) == 2, a
    # a lone low-severity concern does NOT block (no agreeableness-flip, but no single-voice tyranny either)
    a2 = aggregate_veto([{"blocker": True, "severity": "low", "finding": "nit"},
                         {"blocker": False, "severity": "none"}], veto_threshold=2)
    assert a2["verdict"] == "PASS" and a2["n_vetoes"] == 0, a2
    # one high veto below threshold 2 -> still PASS (needs corroboration)
    assert aggregate_veto([{"blocker": True, "severity": "high", "finding": "x"}], 2)["verdict"] == "PASS"
    assert _first_json('noise {"blocker":true,"severity":"high","finding":"f"} tail')["severity"] == "high"
    # high-stakes detection: closer-only and explicit risk metadata only.
    assert not is_high_stakes({"lane": "opener", "labels": ["high-risk"], "title": "auth migration"})
    assert not is_high_stakes({"lane": "closer", "labels": ["routine"], "title": "update copy"})
    assert is_high_stakes({"lane": "closer", "labels": ["risk:high"], "title": "update copy"})
    assert is_high_stakes({"lane": "closer", "labels": ["routine"], "title": "security fix"})
    assert reviewers_from_env({}) == list(DEFAULT_REVIEWERS)
    assert reviewers_from_env({"ORCH_ADVERSARIAL_REVIEWERS": "vibe, gemini"}) == ["vibe", "gemini"]
    assert review_enabled({"ORCH_RUN_ADVERSARIAL_REVIEW": "1"})
    assert not review_enabled({"ORCH_RUN_ADVERSARIAL_REVIEW": "0"})
    print("adversarial.py selftest: OK (refute prompt, minority-veto aggregation, json extract, "
          "high-stakes detection, env helpers)")


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
        _selftest(); return 0
    print("usage: adversarial.py --selftest  (review() is called by the orchestrator/scheduler)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
