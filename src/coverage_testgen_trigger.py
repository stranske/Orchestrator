#!/usr/bin/env python3
"""coverage_testgen_trigger.py — decide when a repository's coverage should buy test-writing.

THE EDGE, NOT THE PARTS. `escaped_defect_priority` ranks where test-writing pays; `testgen_lane`
builds the gate-backed prompt. Nothing read a repository's measured coverage and decided whether
to invoke the lane at all, so both halves waited on somebody remembering.

TWO THRESHOLDS, AND THEY ARE NOT THE SAME KIND OF THING.

  * Below 90 the MACHINE acts: emit a lane invocation for the highest-ranked target.
  * Below 85 a HUMAN is told, once.

WHY "ONCE" IS LOAD-BEARING, with the arithmetic that decided it. Four of the twelve in-scope
repositories are below 85 today. Warning on every cycle while a repo sits there is 4 x 52 = 208
notices a year for four facts already known — about 1.7 hours of reading whose entire content is
"still below the line". That does not overflow a budget so much as train its reader to ignore the
channel, and a channel nobody reads is worse than none. Warning on the CROSSING instead — a repo
that WAS at or above 85 and now is not — is roughly one notice a month at a generous estimate,
half a minute each. Steady-state silence is the correct output for a known gap; the machine keeps
working on it either way, which is the point of the lower threshold being machine-only.

So the warning is FYI, non-blocking, and structurally incapable of accumulating: it fires on a
transition, and a transition that is never acknowledged simply does not fire again.

MEASURED, NEVER ASSUMED. A repository whose coverage cannot be read is reported `unknown` and
buys NOTHING — no invocation, no warning. Treating an unreadable figure as 0 would trigger
test-writing hardest exactly where the measurement is broken, which is the failure this whole
programme has been unwinding. `unknown` and `0.0%` are different findings and only one of them is
about the code.

THIS MODULE DECIDES AND EMITS. It does not dispatch, and it does not write to the Brain. The lane
prompt it produces is consumed by a delegated agent behind `testgen_gate`, and the outcome that
matters — did coverage actually rise — is recorded where outcomes already live.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

KILL_SWITCH = "ORCH_COVERAGE_TESTGEN"

# The owner's stated policy: write tests below 90, warn below 85.
THRESHOLD_WRITE = 90.0
THRESHOLD_WARN = 85.0

ACTION_NONE = "none"
ACTION_WRITE = "write_tests"
ACTION_UNKNOWN = "unknown"


@dataclass
class Decision:
    """What a single repository's coverage buys, and why."""

    repo: str
    coverage: float | None
    previous: float | None = None
    action: str = ACTION_NONE
    warn_human: bool = False
    reason: str = ""
    targets: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "coverage": self.coverage,
            "previous": self.previous,
            "action": self.action,
            "warn_human": self.warn_human,
            "reason": self.reason,
            "targets": self.targets,
        }


def decide(repo: str, coverage: float | None, previous: float | None = None) -> Decision:
    """Pure: coverage in, action out. No I/O, so the policy is testable on its own.

    `previous` is what the repo measured last time this ran. It is used ONLY to decide whether a
    human is told, never whether the machine acts — the machine's threshold is a level, the
    human's is a crossing.
    """
    if coverage is None:
        return Decision(
            repo=repo,
            coverage=None,
            previous=previous,
            action=ACTION_UNKNOWN,
            warn_human=False,
            reason=(
                "coverage could not be read — no invocation and no warning. An unreadable figure "
                "is not a low one, and treating it as 0 would buy the most test-writing exactly "
                "where the measurement is broken"
            ),
        )

    if coverage >= THRESHOLD_WRITE:
        return Decision(
            repo=repo,
            coverage=coverage,
            previous=previous,
            action=ACTION_NONE,
            reason=f"{coverage:.2f}% is at or above the {THRESHOLD_WRITE:.0f}% target",
        )

    # Below 90: the machine acts, whatever the human threshold says.
    crossed = coverage < THRESHOLD_WARN and previous is not None and previous >= THRESHOLD_WARN
    if coverage < THRESHOLD_WARN:
        if crossed:
            reason = (
                f"{coverage:.2f}% CROSSED below {THRESHOLD_WARN:.0f}% (was {previous:.2f}%) — "
                "test-writing queued and the owner told once"
            )
        elif previous is None:
            reason = (
                f"{coverage:.2f}% is below {THRESHOLD_WARN:.0f}% and there is no previous "
                "reading, so this is a first measurement rather than an observed regression; "
                "test-writing queued, no warning"
            )
        else:
            reason = (
                f"{coverage:.2f}% is below {THRESHOLD_WARN:.0f}% and was already "
                f"({previous:.2f}%) — test-writing queued, no repeat warning"
            )
    else:
        reason = f"{coverage:.2f}% is below the {THRESHOLD_WRITE:.0f}% target — test-writing queued"

    return Decision(
        repo=repo,
        coverage=coverage,
        previous=previous,
        action=ACTION_WRITE,
        warn_human=crossed,
        reason=reason,
    )


def coverage_from_report(path: str | Path) -> tuple[float | None, str]:
    """Read a coverage.py JSON total, and say why when it cannot be read.

    Returns `(percent, status)`. Every failure is NAMED: the caller must be able to tell a repo
    with poor coverage from one whose report is missing, unreadable, or the wrong shape.
    """
    report = Path(path).expanduser()
    if not report.exists():
        return None, f"no coverage report at {report}"
    try:
        payload = json.loads(report.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"coverage report {report} is unreadable ({exc})"
    totals = payload.get("totals")
    if not isinstance(totals, dict) or "percent_covered" not in totals:
        return None, f"coverage report {report} carries no totals.percent_covered"
    try:
        return float(totals["percent_covered"]), "ok"
    except (TypeError, ValueError) as exc:
        return None, f"coverage report {report} has a non-numeric percent_covered ({exc})"


def enabled() -> bool:
    """The kill switch. Unset or 0 means decide nothing and emit nothing."""
    return os.environ.get(KILL_SWITCH, "") == "1"


def _capability_heartbeat(event_type: str = "invocation") -> None:
    """Record that the trigger ran, at its own code path. Never raises."""
    try:
        import capabilities

        capabilities.production_heartbeat(
            "coverage-testgen-trigger", event_type, ref="coverage_testgen_trigger.main"
        )
    except Exception:
        pass


def _selftest() -> None:
    # --- the policy, at every boundary --------------------------------------------------------
    assert decide("r", 91.0).action == ACTION_NONE
    assert decide("r", 90.0).action == ACTION_NONE, "the target itself is not below the target"
    assert decide("r", 89.99).action == ACTION_WRITE
    assert decide("r", 86.0).warn_human is False, "between the thresholds is machine-only"

    # --- the crossing, which is the whole human-cost argument ---------------------------------
    assert decide("r", 84.0, previous=86.0).warn_human is True, "a crossing must be told"
    assert decide("r", 84.0, previous=80.0).warn_human is False, "already below: no repeat"
    assert decide("r", 84.0, previous=None).warn_human is False, "first reading is not a fall"
    # Still working on it either way — the machine threshold is a level, not a transition.
    assert decide("r", 84.0, previous=80.0).action == ACTION_WRITE

    # --- unknown is never zero ----------------------------------------------------------------
    unknown = decide("r", None)
    assert unknown.action == ACTION_UNKNOWN, unknown
    assert unknown.warn_human is False
    assert "not a low one" in unknown.reason
    zero = decide("r", 0.0)
    assert zero.action == ACTION_WRITE and zero.action != unknown.action

    # --- reading a report names every failure -------------------------------------------------
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        pct, status = coverage_from_report(base / "absent.json")
        assert pct is None and "no coverage report" in status, status
        bad = base / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        pct, status = coverage_from_report(bad)
        assert pct is None and "unreadable" in status, status
        wrong = base / "wrong.json"
        wrong.write_text(json.dumps({"files": {}}), encoding="utf-8")
        pct, status = coverage_from_report(wrong)
        assert pct is None and "no totals.percent_covered" in status, status
        good = base / "good.json"
        good.write_text(json.dumps({"totals": {"percent_covered": 76.72}}), encoding="utf-8")
        pct, status = coverage_from_report(good)
        assert pct == 76.72 and status == "ok", (pct, status)

    print(
        "coverage_testgen_trigger.py selftest: OK (thresholds at every boundary, warning only on "
        "a crossing, unknown distinguished from zero, and every unreadable report named)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--repo", default="", help="repository name, for the report")
    parser.add_argument("--coverage-json", help="coverage.py JSON report to read the total from")
    parser.add_argument(
        "--previous",
        type=float,
        default=None,
        help="the previous reading, used ONLY to decide whether a human is told",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else sys.argv[1:])

    if args.selftest:
        _selftest()
        return 0

    _capability_heartbeat()

    if not enabled():
        note = f"coverage_testgen_trigger: {KILL_SWITCH} is not set to 1 — deciding nothing"
        print(json.dumps({"skipped": note}) if args.json else note)
        return 0

    if not args.coverage_json:
        parser.error("--coverage-json is required")

    coverage, status = coverage_from_report(args.coverage_json)
    decision = decide(args.repo or "<unnamed>", coverage, args.previous)
    if coverage is None:
        # Carry the READING failure into the decision, so a reader is told which report and why
        # rather than only that the answer was unknown.
        decision.reason = f"{decision.reason} [{status}]"

    payload = decision.as_dict()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"{decision.repo}: {decision.action} — {decision.reason}")
        if decision.warn_human:
            print("  WARN: this repository fell below the warning threshold since the last run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
