#!/usr/bin/env python3
"""redirect_policy.py - advisory retry/decompose policy for watched lanes.

`watch.py` classifies the current lane. This module adds history-aware judgment:
wait, collect, inspect, redirect, or decompose. It is deliberately read-only and
never kills processes, releases claims, or applies labels.
"""

from __future__ import annotations

import json
import sys
from typing import Any

ACTIONS = ("wait", "collect", "inspect", "redirect", "decompose")
REDIRECT_HINTS = {"auth", "rate_limit", "fatal", "network"}
DRIFT_SEVERITIES = {"medium", "high"}
DRIFT_FINDINGS = {"unexpected_paths", "high_risk_paths", "broad_churn"}
DECOMPOSE_THRESHOLD = 2


def _hint_kinds(report: dict) -> set[str]:
    return {str(h.get("kind")) for h in report.get("hints", []) if h.get("kind")}


def _has_redirect_hint(report: dict) -> bool:
    return bool(_hint_kinds(report) & REDIRECT_HINTS)


def _has_drift(report: dict) -> bool:
    drift = report.get("drift") or {}
    if drift.get("severity") in DRIFT_SEVERITIES:
        return True
    kinds = {f.get("kind") for f in drift.get("findings", []) if f.get("kind")}
    return bool(kinds & DRIFT_FINDINGS)


def _summary(history: list[dict]) -> dict:
    return {
        "prior_attempts": len(history),
        "prior_stalls": sum(1 for item in history if item.get("state") == "stalled"),
        "prior_drifts": sum(1 for item in history if _has_drift(item)),
        "prior_redirect_causes": sum(1 for item in history if _has_redirect_hint(item)),
        "prior_actions": [
            item.get("recommended_action") for item in history if item.get("recommended_action")
        ],
    }


def _decision(
    action: str, reason: str, confidence: str, base_action: str, history: list[dict]
) -> dict[str, Any]:
    return {
        "action": action,
        "reason": reason,
        "confidence": confidence,
        "escalated": action != base_action,
        "history_summary": _summary(history),
        "advisory": True,
    }


def decide(report: dict, attempt_history: list[dict] | None = None) -> dict[str, Any]:
    """Return an advisory next action for a watch.py report plus prior reports."""
    # Credit where the DRIVER actually enters this module. The heartbeat previously sat
    # only in main(), which no driver calls -- dispatcher/tick call this function
    # directly -- so the capability ran constantly and recorded nothing. (2026-08-20)
    _capability_heartbeat()
    history = attempt_history or []
    state = report.get("state") or "missing"
    base_action = report.get("recommended_action") or "inspect"
    signals = report.get("signals") or {}
    errors = report.get("errors") or []

    if state == "missing":
        detail = (
            f"classification errors: {', '.join(errors[:3])}"
            if errors
            else "no valid monitor signals"
        )
        return _decision("inspect", detail, "low", base_action, history)

    if state == "exited":
        if signals.get("has_worktree_changes"):
            return _decision(
                "collect", "agent exited after producing changes", "high", base_action, history
            )
        return _decision(
            "inspect", "agent exited without visible changes", "medium", base_action, history
        )

    if state in {"running", "progress"}:
        if _has_drift(report):
            return _decision(
                "inspect",
                "lane is active but drift signals require scope review",
                "high",
                base_action,
                history,
            )
        return _decision(
            "wait", "lane is active without drift or root-cause hints", "high", base_action, history
        )

    if state == "stalled":
        summary = _summary(history)
        if _has_redirect_hint(report):
            if summary["prior_redirect_causes"] >= DECOMPOSE_THRESHOLD:
                return _decision(
                    "decompose",
                    "repeated redirect-worthy failures; narrow the task before retrying",
                    "medium",
                    base_action,
                    history,
                )
            causes = ", ".join(sorted(_hint_kinds(report) & REDIRECT_HINTS))
            return _decision(
                "redirect",
                f"stalled with redirect-worthy root cause: {causes}",
                "high",
                base_action,
                history,
            )
        if _has_drift(report):
            if summary["prior_drifts"] >= DECOMPOSE_THRESHOLD:
                return _decision(
                    "decompose",
                    "repeated drift; split the task and retry with tighter scope",
                    "medium",
                    base_action,
                    history,
                )
            return _decision(
                "inspect",
                "stalled with drift; inspect against acceptance criteria",
                "high",
                base_action,
                history,
            )
        if summary["prior_stalls"] >= DECOMPOSE_THRESHOLD:
            return _decision(
                "decompose",
                "repeated stalls without a clear root cause; task is likely too broad",
                "medium",
                base_action,
                history,
            )
        return _decision(
            "inspect",
            "stalled without a clear root cause; inspect before redirecting",
            "medium",
            base_action,
            history,
        )

    return _decision(
        base_action,
        f"defaulted to watch recommendation: {base_action}",
        "low",
        base_action,
        history,
    )


def _selftest() -> None:
    def report(state: str, **kwargs) -> dict:
        base = {
            "state": state,
            "recommended_action": kwargs.pop("recommended_action", "inspect"),
            "signals": {},
            "hints": [],
            "drift": {"severity": "none", "findings": []},
            "errors": [],
        }
        base.update(kwargs)
        return base

    assert decide(report("progress", recommended_action="wait"))["action"] == "wait"
    assert (
        decide(
            report("exited", signals={"has_worktree_changes": True}, recommended_action="collect")
        )["action"]
        == "collect"
    )
    assert (
        decide(
            report("exited", signals={"has_worktree_changes": False}, recommended_action="collect")
        )["action"]
        == "inspect"
    )
    auth = report("stalled", hints=[{"kind": "auth"}])
    assert decide(auth)["action"] == "redirect"
    drift = report(
        "progress",
        recommended_action="wait",
        drift={"severity": "medium", "findings": [{"kind": "unexpected_paths"}]},
    )
    d = decide(drift)
    assert d["action"] == "inspect" and d["escalated"], d
    two_stalls = [report("stalled"), report("stalled")]
    assert decide(report("stalled"), two_stalls)["action"] == "decompose"
    two_drifts = [drift, drift]
    assert decide(report("stalled", drift=drift["drift"]), two_drifts)["action"] == "decompose"
    two_auth = [auth, auth]
    assert decide(auth, two_auth)["action"] == "decompose"
    missing = decide(report("missing", errors=["need a log"]))
    assert missing["action"] == "inspect" and missing["confidence"] == "low", missing
    assert set(ACTIONS) == {
        decide(report("progress", recommended_action="wait"))["action"],
        decide(report("exited", signals={"has_worktree_changes": True}))["action"],
        decide(report("stalled"))["action"],
        decide(auth)["action"],
        decide(report("stalled"), two_stalls)["action"],
    }
    print("redirect_policy.py selftest: OK (actions, root causes, drift, history escalation)")


def _capability_heartbeat(event_type: str = "invocation") -> None:
    """Record that this infrastructure capability ran. Infra is never ROUTED to — it runs as part
    of the tick — so it records use at its own entrypoint. Lazy import, never raises, and inert
    outside an active tick (ORCH_CAPABILITY_HEARTBEATS). (2026-08-09)"""
    try:
        import capabilities

        capabilities.production_heartbeat("redirect-policy", event_type, ref="redirect_policy.main")
    except Exception:
        pass


def main(argv: list[str]) -> int:
    _capability_heartbeat()
    if "--selftest" in argv:
        _selftest()
        return 0
    if "--json" in argv:
        payload = json.load(sys.stdin)
        print(
            json.dumps(
                decide(payload.get("report", payload), payload.get("attempt_history")), indent=2
            )
        )
        return 0
    print("usage: redirect_policy.py --selftest | --json < payload.json", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
