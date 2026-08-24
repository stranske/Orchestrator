#!/usr/bin/env python3
"""Authoritative cadence-step registry shared by orchestrate.sh and reports.

The shell owns execution.  This module owns step identity, success/failure stamp
names, cadence, evidence artifacts, and safe next transitions so operator reports
do not have to reverse-engineer shell prose.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

CADENCE_STEPS: tuple[dict[str, Any], ...] = (
    {
        "key": "capability-lifecycle",
        "success_stamp": None,
        "cadence_days": 0,
        "artifact": "capability-validation.json",
        "log": "capability-lifecycle.log",
        "gate": "active tick only; lifecycle validation must pass",
        "next_transition": "retry validation after backoff; invalid active declarations block dispatch",
    },
    {
        "key": "pattern-miner",
        "success_stamp": ".last-pattern-miner",
        "cadence_days": 1,
        "artifact": "pattern-miner-inventory.json",
        "log": "pattern-miner.log",
        "gate": "accepted redacted completion episodes available",
        "next_transition": "retry mining after backoff; candidates expire automatically",
    },
    {
        "key": "evidence-acquisition",
        "success_stamp": ".last-evidence-acquisition",
        "cadence_days": 1,
        "artifact": "evidence-acquisition-plan.json",
        "log": "evidence-acquisition.log",
        "gate": "a capability unblock() marks feedable; a documented default-off switch is never fed",
        "next_transition": "stays shadow-only until ORCH_EVIDENCE_ACQUISITION=1; feedable 0 is the "
        "honest state while every starved capability is held by a default-off gate",
    },
    {
        "key": "keepalive-stage2-plan",
        "success_stamp": ".last-keepalive-stage2-plan",
        "cadence_days": 0,
        "artifact": "keepalive-supervisor-stage2-plan.json",
        "log": None,
        "gate": "GitHub search and core capacity",
        "next_transition": "retry on next due tick when GitHub capacity is available",
    },
    {
        "key": "keepalive-ingest",
        "success_stamp": ".last-keepalive-ingest",
        "cadence_days": 0,
        "artifact": None,
        "log": "keepalive-ingest.log",
        "gate": "GitHub core capacity",
        "next_transition": "retry outcome ingest after backoff",
    },
    {
        "key": "local-outcomes-ingest",
        "success_stamp": ".last-local-outcomes-ingest",
        "cadence_days": 0,
        "artifact": None,
        "log": None,
        "gate": "GitHub core capacity",
        "next_transition": "retry local outcome join after backoff",
    },
    {
        # Pure local join (no gh), so it has no capacity gate: run outcomes -> capability ledger.
        # Without it capabilities record that they RAN but never how the work turned out, and every
        # gate reads as starved regardless of real evidence (2026-08-09).
        "key": "capability-outcome-bridge",
        "success_stamp": ".last-capability-outcome-bridge",
        "cadence_days": 0,
        "artifact": None,
        "log": "capability-outcome-bridge.log",
        "gate": None,
        "next_transition": "retry capability outcome propagation after backoff",
    },
    {
        # Pure local (no gh): link applied-redirect outcomes, then the self-gated apply. Exists
        # because redirect_plan.apply_plan had zero callers and the Stage-2 gate can only be fed by
        # applied advice, which made it a structural deadlock (2026-08-21).
        "key": "redirect-apply-link",
        "success_stamp": ".last-redirect-apply-link",
        "cadence_days": 0,
        "artifact": None,
        "log": "redirect-apply.log",
        "gate": "apply requires ORCH_REDIRECT_APPLY_BOOTSTRAP=1; linking is unconditional",
        "next_transition": "retry the outcome link after backoff; the bootstrap self-disables once "
        "the Stage-2 deficits close",
    },
    {
        "key": "capability-propensity",
        "success_stamp": ".last-capability-propensity",
        "cadence_days": 6,
        "artifact": "capability-propensity.json",
        "log": "capability-propensity.log",
        "gate": "none; pure read of the capability ledger. "
        "ORCH_CAPABILITY_PROPENSITY_DISABLED=1 stops the advisor ranking",
        "next_transition": "while capabilities_with_evidence is 0 every propensity is the PRIOR, "
        "not a measurement, and the step says so on every run. It becomes a "
        "measurement when callers record trigger/usefulness against the "
        "advice:<digest> they were given",
    },
    {
        # EVERY TICK, and deliberately stampless (like capability-lifecycle): the step is a cheap
        # advisory consult plus an idempotent verdict, and its own bounding comes from the artifact
        # freshness of the capabilities it grades, not from a cadence stamp. A stamp here would only
        # add a second, drifting notion of "due".
        "key": "tick-capability-evidence",
        "success_stamp": None,
        "cadence_days": 0,
        "artifact": "tick-capability-evidence.json",
        "log": "tick-capability-evidence.log",
        "gate": "ORCH_TICK_EVIDENCE_DISABLED=1 or ORCH_DISABLE_STEPS=tick-capability-evidence "
        "makes it inert; a verdict additionally requires the graded capability's own "
        "cadence artifact to have been regenerated since the last evaluation",
        "next_transition": "records at most one verdict per bound capability per UTC day; while "
        "`gradable` is non-zero and `verdicts_recorded` is 0 the step is waiting "
        "on those capabilities' own cadences, and a `gradable` of 0 is a "
        "deadlock rather than patience",
    },
    {
        # EVERY TICK, deliberately stampless for the same reason as `tick-capability-evidence`
        # above: the step is a cheap advisory consult, and its bounding comes from the per-(surface,
        # UTC day) consult digest that makes the match heartbeat idempotent, not from a stamp. It
        # exists because the tick's FOURTEEN phase-bound capabilities had a declared binding and no
        # caller -- a binding nothing consults can never be selected, so it can never earn the
        # evidence that would rank it. Registered here (rather than special-cased in the shell) so
        # `ORCH_DISABLE_STEPS=tick-phase-consult` is a control that actually works: an unregistered
        # key WARNs "nothing was disabled by it" while silently disabling the step, which is a
        # control that lies.
        "key": "tick-phase-consult",
        "success_stamp": None,
        "cadence_days": 0,
        "artifact": None,
        "log": "tick-phase-consult.log",
        "gate": "ORCH_DISABLE_STEPS=tick-phase-consult makes it inert; it records no verdict of any "
        "kind, so the tick-capability-evidence verdict ceiling is unaffected",
        "next_transition": "writes at most one advisory `match` event per bound capability per "
        "phase per UTC day (first tick of the day; nothing on the other 23). A "
        "phase reporting `offered 0` is a broken binding, not a quiet one",
    },
    {
        "key": "capability-firing-monitor",
        "success_stamp": ".last-capability-firing-monitor",
        "cadence_days": 6,
        "artifact": "capability-firing-monitor.json",
        "log": "capability-firing-monitor.log",
        "gate": "none; read-only apart from its own history file. "
        "ORCH_FIRING_MONITOR_DISABLED=1 stops the write",
        "next_transition": "the regression alarm needs two snapshots, so the first run only "
        "establishes a baseline; from the second it reports any capability that "
        "used to fire and stopped",
    },
    {
        "key": "switch-review",
        "success_stamp": ".last-switch-review",
        "cadence_days": 6,
        "artifact": "switch-review.json",
        "log": "switch-review.log",
        "gate": "writes require ORCH_SWITCH_REVIEW=1; report-only otherwise",
        "next_transition": "re-raise held/idle switches weekly; questions auto-ratify to the "
        "conservative default so no backlog can form",
    },
    {
        "key": "feature-scan",
        "success_stamp": ".last-feature-scan",
        "cadence_days": 0,
        "artifact": "feature-scan.json",
        "log": "feature-scan.log",
        "gate": None,
        "next_transition": "retry the feature scan after backoff; report-only, writes require --apply",
    },
    {
        "key": "capability-activation-audit",
        "success_stamp": ".last-capability-activation-audit",
        "cadence_days": 0,
        "artifact": "capability-activation.json",
        "log": "capability-activation-audit.log",
        "gate": None,
        "next_transition": "retry the activation audit after backoff; read-only, never blocks a tick",
    },
    {
        "key": "issue-readiness",
        "success_stamp": ".last-issue-readiness",
        # 0 is this registry's spelling of "daily": _due() uses `find -mtime +N`, so +0 fires once
        # the stamp is >24h old. Declaring 1 here would fire only after >48h.
        "cadence_days": 0,
        "artifact": "issue-readiness.json",
        "log": "issue-readiness.log",
        "gate": "GitHub search+core capacity; writes require ORCH_ISSUE_AUTOREADY=1",
        "next_transition": "retry readiness assessment after backoff; unreviewed risk issues "
        "auto-ratify to ready at owner-question expiry, so nothing stalls",
    },
    {
        "key": "durability-sweep",
        "success_stamp": ".last-durability-sweep",
        "cadence_days": 0,
        "artifact": None,
        "log": None,
        "gate": "GitHub search capacity",
        "next_transition": "retry pending durability resolution after backoff",
    },
    {
        "key": "capability-causal-reconcile",
        "success_stamp": ".last-capability-causal-reconcile",
        "cadence_days": 0,
        "artifact": "capability-validation.json",
        "log": "capability-lifecycle.log",
        "gate": "immutable capability versions and exact completion/outcome joins",
        "next_transition": "retry causal reconciliation after outcomes and durability land",
    },
    {
        "key": "langsmith-direct",
        "success_stamp": ".last-langsmith-direct",
        "cadence_days": 0,
        "artifact": None,
        "log": None,
        "gate": "LANGSMITH_API_KEY present",
        "next_transition": "retry direct cost and trace pull after backoff",
    },
    {
        "key": "ledger-reconcile",
        "success_stamp": ".last-ledger-reconcile",
        "cadence_days": 0,
        "artifact": None,
        "log": None,
        "gate": "local ledger available",
        "next_transition": "retry strict local ledger reconciliation after backoff",
    },
    {
        "key": "ccusage-reconcile",
        "success_stamp": ".last-ccusage-reconcile",
        "cadence_days": 0,
        "artifact": None,
        "log": None,
        "gate": "ccusage attribution available",
        "next_transition": "retry per-run attribution after backoff",
    },
    {
        "key": "range-rollout",
        "success_stamp": ".last-range-rollout",
        "cadence_days": 0,
        "artifact": "range-rollout.json",
        "log": "range-rollout.log",
        "gate": "eligible opener, unclaimed target, capacity, cap, and rollout kill switch",
        "next_transition": "retry preview after backoff; active dispatch remains separately guarded",
    },
    {
        "key": "runtime-ac-flow",
        "success_stamp": ".last-runtime-ac-flow",
        "cadence_days": 0,
        "artifact": "runtime-ac-flow-monitor.json",
        "log": "runtime-ac-flow-monitor.log",
        "gate": "structured runtime-AC gate events in the canonical feedback event plane",
        "next_transition": "retry monitor after backoff; repair exact missing-spec or execution reason",
    },
    {
        "key": "relearn",
        "success_stamp": ".last-relearn",
        "cadence_days": 6,
        "artifact": None,
        "log": None,
        "gate": "weekly learning cadence",
        "next_transition": "retry versioned route-weight learning after backoff",
    },
    {
        "key": "periodic-report",
        "success_stamp": ".last-periodic-report",
        "cadence_days": 6,
        "artifact": "periodic-report.json",
        "log": None,
        "gate": "weekly operator cadence",
        "next_transition": "retry report and dashboard generation after backoff",
    },
    {
        "key": "keepalive-shadow",
        "success_stamp": ".last-keepalive-shadow",
        "cadence_days": 0,
        "artifact": None,
        "log": None,
        "gate": "GitHub search capacity",
        "next_transition": "retry advisory shadow collection after backoff",
    },
    {
        "key": "keepalive-backfill",
        "success_stamp": ".last-keepalive-backfill",
        "cadence_days": 6,
        "artifact": None,
        "log": None,
        "gate": "GitHub search capacity",
        "next_transition": "retry resolved shadow backfill after backoff",
    },
    {
        "key": "consumer-sync-artifact-ingest",
        "success_stamp": ".last-consumer-sync-artifact-ingest",
        "cadence_days": 0,
        "artifact": "consumer-sync-artifact-ingest-report.json",
        "log": "consumer-sync-artifact-ingest.log",
        "gate": "GitHub core capacity and active tick",
        "next_transition": "retry consumer sync artifact ingestion after backoff",
    },
)

STEP_BY_KEY = {row["key"]: row for row in CADENCE_STEPS}


def _mtime(path: Path) -> int | None:
    try:
        return int(path.stat().st_mtime)
    except OSError:
        return None


def inspect_cadence(
    state_dir: Path,
    *,
    now: int | None = None,
    retry_hours: int | None = None,
    registry: tuple[dict[str, Any], ...] | None = None,
) -> dict:
    current = int(time.time()) if now is None else int(now)
    retry_h = int(
        os.environ.get("ORCH_CADENCE_RETRY_HOURS", "6") if retry_hours is None else retry_hours
    )
    steps = []
    for declared in registry or CADENCE_STEPS:
        row = dict(declared)
        key = row["key"]
        success_path = state_dir / row["success_stamp"] if row.get("success_stamp") else None
        failure_path = state_dir / f".fail-{key}"
        success_ts = _mtime(success_path) if success_path else None
        failure_ts = _mtime(failure_path)
        success_age = max(0, current - success_ts) if success_ts is not None else None
        failure_age = max(0, current - failure_ts) if failure_ts is not None else None
        stale_after_s = (int(row.get("cadence_days") or 0) + 1) * 86400 + 12 * 3600
        if success_path is None:
            success_status = "not_applicable"
        elif success_ts is None:
            success_status = "missing"
        else:
            success_status = "stale" if success_age > stale_after_s else "fresh"
        try:
            failure_count = int(failure_path.read_text().strip()) if failure_ts is not None else 0
        except (OSError, ValueError):
            failure_count = 1 if failure_ts is not None else 0
        retry_after_s = max(0, retry_h * 3600 - int(failure_age or 0)) if failure_count else 0
        if failure_count:
            retry_state = "backoff" if retry_after_s else "ready_to_retry"
            exact_reason = f"{key} failed {failure_count} consecutive attempt(s)"
        else:
            retry_state = "none"
            exact_reason = "no recorded cadence failure"
        steps.append(
            {
                **row,
                "success_path": str(success_path) if success_path else None,
                "failure_path": str(failure_path),
                "last_success_ts": success_ts,
                "success_age_s": success_age,
                "success_status": success_status,
                "failure_count": failure_count,
                "last_failure_ts": failure_ts,
                "failure_age_s": failure_age,
                "retry_state": retry_state,
                "retry_after_s": retry_after_s,
                "exact_reason": exact_reason,
                "artifact_path": str(state_dir / row["artifact"]) if row.get("artifact") else None,
                "log_path": str(state_dir / row["log"]) if row.get("log") else None,
            }
        )
    failed = [row for row in steps if row["failure_count"]]
    durability = next((row for row in steps if row["key"] == "durability-sweep"), {})
    return {
        "state_dir": str(state_dir),
        "retry_hours": retry_h,
        "step_count": len(steps),
        "failed_step_count": len(failed),
        "backoff_step_count": sum(row["retry_state"] == "backoff" for row in failed),
        "ready_to_retry_count": sum(row["retry_state"] == "ready_to_retry" for row in failed),
        "steps": steps,
        # Compatibility fields retained while callers migrate to the all-step rows.
        "durability_sweep_stamp": durability.get("success_path"),
        "durability_sweep_stamp_status": durability.get("success_status"),
        "durability_sweep_stamp_age_s": durability.get("success_age_s"),
        "durability_sweep_stale_after_s": (int(durability.get("cadence_days") or 0) + 1) * 86400
        + 12 * 3600,
    }


def shell_functions() -> str:
    """Emit constant-only Bash functions consumed by orchestrate.sh."""
    stamp_cases = []
    day_cases = []
    known_cases = []
    for row in CADENCE_STEPS:
        key = row["key"]
        stamp = row.get("success_stamp") or ""
        stamp_cases.append(f"    {key}) printf '%s\\n' '{stamp}' ;;")
        day_cases.append(f"    {key}) printf '%s\\n' '{int(row.get('cadence_days') or 0)}' ;;")
        known_cases.append(f"    {key}) return 0 ;;")
    return "\n".join(
        [
            'cadence_stamp() { case "$1" in',
            *stamp_cases,
            "    *) return 2 ;;",
            "  esac; }",
            'cadence_days() { case "$1" in',
            *day_cases,
            "    *) return 2 ;;",
            "  esac; }",
            'cadence_known() { case "$1" in',
            *known_cases,
            "    *) return 2 ;;",
            "  esac; }",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("json", "shell"), nargs="?", default="json")
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".codex/orchestrator")
    args = parser.parse_args(argv)
    if args.command == "shell":
        print(shell_functions())
    else:
        print(json.dumps(inspect_cadence(args.state_dir), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
