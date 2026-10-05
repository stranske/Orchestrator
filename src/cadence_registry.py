#!/usr/bin/env python3
"""Authoritative cadence-step registry shared by orchestrate.sh and reports.

The shell owns execution.  This module owns step identity, success/failure stamp
names, cadence, evidence artifacts, whether a step is retired by default, which
capabilities a step runs (where a row declares it), and safe next transitions so
operator reports do not have to reverse-engineer shell prose.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# `cadence_days` IS NOT THE PERIOD. It is N in `find STAMP -mtime +N`, which is how `_due()` in
# orchestrate.sh decides a stamped step is due, and `find` counts WHOLE days since the stamp and
# discards the remainder: `+N` matches only once the stamp is at least N+1 days old. So 0 is daily,
# 1 is every other day, and a row writes one less than the period it means. That rule used to live
# in a comment on the issue-readiness row, and the rows written after it never saw it: pattern-miner,
# fleet-shapes, agent-switches and evidence-acquisition declared 1 for steps their docs and their own
# `[cadence] ... (daily ...)` log lines call daily, and ran 48-50h apart (tick log, 2026-09-11..22);
# coverage-testgen-trigger declared 7 for weekly and ran every eight days. So it is a test now:
# tests/test_cadence_period.py replays the real `_due` and `_cadence_due` against aged stamps, fails
# a stamped row whose value is not named below, and fails a row whose `[cadence]` log line names a
# different period. A genuinely new period is added here, by name, never as a bare number on a row.
CADENCE_DAYS_FOR: dict[str, int] = {
    "daily": 0,  # due once the stamp is 24h old; at the hourly tick a daily step runs every ~25h
    "weekly": 6,  # due once the stamp is 7 days old
}

# A STEP CAN BE RETIRED BY DEFAULT, AND THAT IS DECLARED HERE, ONCE, ON ITS ROW. `retired` says the
# tick skips the step unless one of `re_enable_env` holds in the tick's environment. Three readers
# take it from the row, and none of them restates it:
#   * orchestrate.sh's `_cadence_due`, through the generated `cadence_retired` (see
#     `shell_functions`): it prints a `[retired]` line on every tick that skips the step and touches
#     no stamp, the same contract as ORCH_DISABLE_STEPS;
#   * `inspect_cadence`, which reports the step `retired`, with the condition that brings it back,
#     instead of `stale`;
#   * `capabilities.KNOWN_DECLARATIONS`, whose `gate_reason` for the step's capability is
#     `declared_gate_reason`, so the ledger's prose is rewritten from this row on every reconciling
#     load instead of being typed into the ledger once and left to age.
# WHY. The issue-readiness retirement (2026-09-15) was a silent `:` in orchestrate.sh behind its own
# copy of the re-enable condition. The row went on describing a live daily step, so the inspector
# called it `stale` from then on (340 h on 2026-10-02): a retired step's stamp only ages, so `stale`
# was the one verdict it could never leave, and a stale count of zero was unreachable. The ledger
# still said the gate was ARMED and "the assessment and the label write both run".
RETIRED_FIELD = "retired"
_RETIRED_KEYS = frozenset({"since", "reason", "re_enable_env"})

# A STEP DECLARES THE CAPABILITIES IT RUNS, by ledger id, under `capabilities`. That is the one link
# from a capability to the step the tick invokes it through, and it is DECLARED, never inferred:
# step keys and capability ids coincide for some steps and not others (`range-rollout` runs
# `range-lane-rollout`, `rail-exercise` runs `rail-exercise-cadence`), so a match on names is right
# only by accident. Its reader is `retirement_holds`, through which `capability_firing_monitor`
# reports a retired step's capabilities as HELD OFF, with the retirement and what lifts it, instead
# of as overdue: the same contract as `inspect_cadence` reporting the step `retired` instead of
# `stale`. A retired row must declare it, empty if it runs none; tests/test_cadence_retirement.py
# holds that, so a retirement cannot leave a capability alarming because nobody wrote the link.
CAPABILITIES_FIELD = "capabilities"
_ENV_NAME_RE = re.compile(r"ORCH_[A-Z0-9_]+")
_ENV_VALUE_RE = re.compile(r"[A-Za-z0-9._-]+")
_DATE_RE = re.compile(r"20\d\d-\d\d-\d\d")

CADENCE_STEPS: tuple[dict[str, Any], ...] = (
    {
        "key": "rail-exercise",
        "capabilities": ("rail-exercise-cadence",),
        "success_stamp": ".last-rail-exercise",
        "cadence_days": 6,
        "artifact": "rail-exercise-report.json",
        "log": "rail-exercise.log",
        "gate": "shadow by default; ORCH_RAIL_EXERCISE_RECORD=1 enables explicit machine-observed ledger evidence",
        "next_transition": "retry contracts next week; skipped contracts name their missing fixture or malformed break case",
    },
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
        "cadence_days": 0,
        "artifact": "pattern-miner-inventory.json",
        "log": "pattern-miner.log",
        "gate": "accepted redacted completion episodes available",
        "next_transition": "retry mining after backoff; candidates expire automatically",
    },
    {
        "key": "fleet-shapes",
        "success_stamp": ".last-fleet-shapes",
        "cadence_days": 0,
        "artifact": "fleet-shapes.json",
        "log": "fleet-shapes.log",
        "gate": "merged agent PRs in the window whose facts gh can return",
        "next_transition": "retry after backoff; a PR gh cannot return stays counted as missing, "
        "never mined as a shape",
    },
    {
        "key": "agent-switches",
        "success_stamp": ".last-agent-switches",
        "cadence_days": 0,
        "artifact": "agent-switches.json",
        "log": "agent-switches.log",
        "gate": "keepalive PRs in the window whose label timeline gh can return",
        "next_transition": "retry after backoff; a PR gh cannot return stays counted as missing",
    },
    {
        "key": "evidence-acquisition",
        "success_stamp": ".last-evidence-acquisition",
        "cadence_days": 0,
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
        "capabilities": ("redirect-apply-bootstrap",),
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
        # EVERY ACTIVE TICK, FIRST, and stampless: it is an observer armed before any step, not a
        # step with a period. Registered so `ORCH_DISABLE_STEPS=tick-watchdog` is a control that
        # works -- the tick then prints, every run, that nothing watches it -- and so the
        # observability dashboard lists the record it writes.
        "key": "tick-watchdog",
        "success_stamp": None,
        "cadence_days": 0,
        "artifact": "tick-watchdog.json",
        "log": None,
        "gate": "ORCH_DISABLE_STEPS=tick-watchdog stops it arming; report-only, it never signals "
        "anything",
        "next_transition": "ALERTs into the tick log when one command passes 90 min or the tick "
        "3 h of awake time, hourly while stuck, each with the `kill` that frees the "
        "tick; the next tick's first line says how the previous one ended",
    },
    {
        "key": "capability-firing-monitor",
        "success_stamp": ".last-capability-firing-monitor",
        "cadence_days": 6,
        "artifact": "capability-firing-monitor.json",
        "log": "capability-firing-monitor.log",
        "gate": "none; read-only apart from its own history file and, in a live tick, its "
        "invocation heartbeat. ORCH_FIRING_MONITOR_DISABLED=1 stops the history write; "
        "ORCH_DISABLE_STEPS=capability-firing-monitor skips the step, so nothing is written",
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
        "cadence_days": 0,
        "artifact": "issue-readiness.json",
        "log": "issue-readiness.log",
        "gate": "GitHub search+core capacity; writes require ORCH_ISSUE_AUTOREADY=1",
        "next_transition": "retry readiness assessment after backoff; unreviewed risk issues "
        "auto-ratify to ready at owner-question expiry, so nothing stalls",
        # Retired with the tool's own dispatch lane (assessment 2026-09-03, item 1): the lanes read
        # capacity.json and never backlog.json, and the label writes are gated off, so 1,350 runs
        # in eleven days fed nothing. Either flag brings it back; the label writes still need
        # ORCH_ISSUE_AUTOREADY=1, as `gate` says.
        "capabilities": ("issue-readiness",),
        "retired": {
            "since": "2026-09-15",
            "reason": "it feeds only this tool's own dispatch lane, which is shadow by default "
            "(1,350 runs in eleven days fed nothing)",
            "re_enable_env": {"ORCH_DISPATCH_LANE": "1", "ORCH_ISSUE_AUTOREADY": "1"},
        },
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
        "key": "research-usage-guard",
        "capabilities": ("research-usage-guard",),
        "success_stamp": ".last-research-usage-guard",
        "cadence_days": 0,
        "artifact": "research-usage-report.json",
        "log": "research-usage-report.log",
        "gate": "local research opportunity ledger available; no network or model calls",
        "next_transition": "retry after backoff while an active budget/anomaly block, stale "
        "dispatch, or telemetry outage remains",
    },
    {
        "key": "route-weights-export",
        "capabilities": ("route-weights-export",),
        "success_stamp": ".last-route-weights-export",
        "cadence_days": 0,
        "artifact": "route-weights-export.json",
        "log": "route-weights-export.log",
        "gate": "ORCH_DISABLE_STEPS=route-weights-export stops the daily export; the cadence passes "
        "--publish (owner decision 2026-09-21) and the script publishes to exports/route-weights only "
        "when ORCH_ROUTE_WEIGHTS_PUBLISH=1 and the remote artifact would change",
        "next_transition": "rewrite the local artifact from the latest route_weights version; "
        "consumer fetch remains fail-open to its static policy",
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
    {
        # WEEKLY, and that is a deliberate ceiling rather than a shrug. The step decides whether a
        # repository's measured coverage buys test-writing; a coverage figure moves when a PR
        # merges, so a daily cadence would re-decide the same number six times out of seven. The
        # HUMAN half is rarer still by construction — it reports a crossing below the warning
        # line, not the fact of being below it, because four of twelve repos are below it today
        # and saying so every cycle is 208 notices a year for four facts already known.
        "key": "coverage-testgen-trigger",
        "success_stamp": ".last-coverage-testgen-trigger",
        "cadence_days": 6,
        "artifact": "coverage-testgen-trigger-report.json",
        "log": "coverage-testgen-trigger.log",
        "gate": "ORCH_COVERAGE_TESTGEN and a readable coverage report",
        "next_transition": "re-read coverage next week; an unreadable report decides nothing",
    },
)

STEP_BY_KEY = {row["key"]: row for row in CADENCE_STEPS}

# Grace past one full period before a step's last success reads as STALE: an hourly tick runs a
# daily step up to an hour late, and a gh-budget skip defers it a tick or two more.
STALE_GRACE_S = 12 * 3600


def stale_after_seconds(row: dict[str, Any]) -> int:
    """How old a step's last success may be before it no longer describes the present.

    ONE rule, consumed by `inspect_cadence` (which calls the step stale) and by any consumer of a
    step's ARTIFACT that must decide whether it is still current — `redirect_apply` reads the
    keepalive supervisor's stage-2 plan as its candidate population, and a plan this rule calls
    stale is not a population at all. Written twice as a literal in `inspect_cadence` until
    2026-10-02; a pair of literals is how a report and its consumer come to disagree.
    """
    return (int(row.get("cadence_days") or 0) + 1) * 86400 + STALE_GRACE_S


def retirement(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """The row's declared retirement, validated and copied; None when it declares none.

    A malformed declaration RAISES. The registry is a code constant, so the only way to reach this
    with a bad one is an edit, and the suite runs this over every row; at runtime the raise
    reaches `cadence_registry.py shell`, and orchestrate.sh aborts loudly on an unreadable
    registry rather than guessing which steps are off.
    """
    declared = row.get(RETIRED_FIELD)
    if declared is None:
        return None
    key = row.get("key")
    if not isinstance(declared, Mapping) or set(declared) != _RETIRED_KEYS:
        raise ValueError(f"{key}: `retired` must carry exactly {sorted(_RETIRED_KEYS)}")
    since, reason, env = declared["since"], declared["reason"], declared["re_enable_env"]
    if not isinstance(since, str) or not _DATE_RE.fullmatch(since):
        raise ValueError(f"{key}: retired.since must be YYYY-MM-DD, got {since!r}")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(f"{key}: retired.reason must say why the step is off")
    # A retirement nothing can lift is a deletion; that is a different change, made by removing the
    # step. So at least one flag must bring it back, and each must be a plain ORCH_ name and value,
    # because both are written into generated shell.
    if not isinstance(env, Mapping) or not env:
        raise ValueError(f"{key}: retired.re_enable_env must name at least one flag")
    for name, value in env.items():
        if not _ENV_NAME_RE.fullmatch(str(name)) or not _ENV_VALUE_RE.fullmatch(str(value)):
            raise ValueError(f"{key}: retired.re_enable_env has an unusable entry {name}={value}")
    return {"since": since, "reason": reason, "re_enable_env": dict(env)}


def re_enable_when(row: Mapping[str, Any]) -> str | None:
    """The flags that bring a retired step back, as an operator would set them."""
    held = retirement(row)
    if held is None:
        return None
    return " or ".join(f"{name}={value}" for name, value in held["re_enable_env"].items())


def retirement_lifted_by(row: Mapping[str, Any], environ: Mapping[str, str]) -> str | None:
    """Which re-enable flag holds in `environ`, as `NAME=value`; None when none does.

    Exact string equality, because that is what the generated shell tests: `ORCH_X=1 ` (with a
    trailing space) lifts nothing in either place.
    """
    held = retirement(row)
    if held is None:
        return None
    for name, value in held["re_enable_env"].items():
        if environ.get(name) == value:
            return f"{name}={value}"
    return None


def retirement_line(row: Mapping[str, Any]) -> str | None:
    """The one sentence the tick prints when it skips a retired step, and the reports quote."""
    held = retirement(row)
    if held is None:
        return None
    return f"retired {held['since']}: {held['reason']}; runs again with {re_enable_when(row)}"


def declared_gate_reason(key: str) -> str:
    """The capability ledger's `gate_reason` for the step `key`, derived from its row.

    Consumed by `capabilities.KNOWN_DECLARATIONS`, so reconciliation rewrites the ledger from the
    registry: a retirement lifted here stops being reported there on the next reconciling load.
    """
    row = STEP_BY_KEY[key]
    line = retirement_line(row)
    if line is None:
        return str(row.get("gate") or "")
    return (
        f"cadence step {key} is {line}. The tick skips it and prints that on every run "
        f"(cadence_registry `{RETIRED_FIELD}`). When it runs: {row.get('gate')}"
    )


def carried_capabilities(row: Mapping[str, Any]) -> tuple[str, ...]:
    """The capability ids the row declares it runs; () when it declares none.

    A malformed declaration RAISES, for the reason `retirement` gives: the registry is a code
    constant, so only an edit can reach this with a bad one, and the suite runs it over every row.
    """
    declared = row.get(CAPABILITIES_FIELD)
    if declared is None:
        return ()
    key = row.get("key")
    if isinstance(declared, (str, bytes)) or not isinstance(declared, (tuple, list)):
        raise ValueError(f"{key}: `{CAPABILITIES_FIELD}` must be a tuple of capability ids")
    ids = tuple(declared)
    for cap_id in ids:
        if not isinstance(cap_id, str) or not cap_id or cap_id != cap_id.strip():
            raise ValueError(f"{key}: `{CAPABILITIES_FIELD}` has an unusable id {cap_id!r}")
    if len(set(ids)) != len(ids):
        raise ValueError(f"{key}: `{CAPABILITIES_FIELD}` names a capability twice: {ids}")
    return ids


def retirement_holds(
    environ: Mapping[str, str] | None = None,
    registry: tuple[dict[str, Any], ...] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """{capability id: the retired steps holding it off}, for every capability a retirement holds.

    A capability is held off when at least one step declares it under `capabilities` and EVERY step
    declaring it is retired with no re-enable flag holding in `environ` (this process's environment
    by default, as for `inspect_cadence`). One live carrier means the tick still runs it, so its
    silence is still a finding and nothing here holds it. Each hold carries the retirement and the
    flags that lift it, so whoever reads it is told what would end it. Raises on a malformed row,
    as `retirement` and `carried_capabilities` do. `registry=None` is `CADENCE_STEPS`; an empty
    registry is empty, never the live one.
    """
    env = os.environ if environ is None else environ
    carriers: dict[str, list[dict[str, Any]]] = {}
    for row in CADENCE_STEPS if registry is None else registry:
        for cap_id in carried_capabilities(row):
            carriers.setdefault(cap_id, []).append(row)
    holds: dict[str, list[dict[str, Any]]] = {}
    for cap_id, rows in sorted(carriers.items()):
        held = []
        for row in rows:
            declared = retirement(row)
            if declared is None or retirement_lifted_by(row, env) is not None:
                break
            held.append(
                {
                    "step": row["key"],
                    **declared,
                    "re_enable_when": re_enable_when(row),
                    "line": retirement_line(row),
                }
            )
        else:
            holds[cap_id] = held
    return holds


def shortest_stale_after_s(registry: tuple[dict[str, Any], ...] | None = None) -> int:
    """The tightest `stale_after_seconds` over the stamped steps -- 36 h while any step is daily."""
    stamped = [row for row in registry or CADENCE_STEPS if row.get("success_stamp")]
    return min((stale_after_seconds(row) for row in stamped), default=stale_after_seconds({}))


def newest_outcome(report: dict[str, Any]) -> dict[str, Any] | None:
    """The most recent outcome ANY step recorded, success stamp or failure marker, in an
    `inspect_cadence` report; None when no step has recorded one on this machine.

    While ticks reach the cadence block, something lands at least every shortest period, so when
    nothing has for longer than `shortest_stale_after_s`, ticks are starting and reaching no
    cadence step at all -- an early abort every tick -- whatever each tick's own output says.
    Every single stamp being old is the WRONG test for that: a weekly stamp is young for days."""
    best: dict[str, Any] | None = None
    for row in report.get("steps") or []:
        for kind, ts in (
            ("success", row.get("last_success_ts")),
            ("failure", row.get("last_failure_ts")),
        ):
            if ts is not None and (best is None or ts > best["ts"]):
                best = {"key": row.get("key"), "kind": kind, "ts": int(ts)}
    return best


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
    environ: Mapping[str, str] | None = None,
) -> dict:
    """Every step's stamp verdict: `fresh`, `stale`, `missing`, `not_applicable` or `retired`.

    `retired` means the row declares a retirement, no re-enable flag holds in `environ` (this
    process's environment by default), and the stamp is not fresh: the tick skips the step and says
    so, and the row's `retired.re_enable_when` names what brings it back. A retired step whose stamp
    IS fresh reads `fresh`: it evidently ran, because the tick's environment lifted the retirement
    or the retirement is newer than the run, and a recent run outranks an environment this process
    may not share with the tick.
    """
    current = int(time.time()) if now is None else int(now)
    retry_h = int(
        os.environ.get("ORCH_CADENCE_RETRY_HOURS", "6") if retry_hours is None else retry_hours
    )
    env = os.environ if environ is None else environ
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
        stale_after_s = stale_after_seconds(row)
        if success_path is None:
            success_status = "not_applicable"
        elif success_ts is None:
            success_status = "missing"
        else:
            # `success_ts is None` was ruled out above, so the age is a real int here; stating it
            # keeps the comparison typed without changing the branch logic.
            success_status = "stale" if int(success_age or 0) > stale_after_s else "fresh"
        held = retirement(declared)
        lifted_by = retirement_lifted_by(declared, env) if held else None
        if held and lifted_by is None and success_status != "fresh":
            success_status = "retired"
        row[RETIRED_FIELD] = (
            None
            if held is None
            else {
                **held,
                "re_enable_when": re_enable_when(declared),
                "lifted_by": lifted_by,
                "line": retirement_line(declared),
            }
        )
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
    retired = [row["key"] for row in steps if row["success_status"] == "retired"]
    return {
        "state_dir": str(state_dir),
        "retry_hours": retry_h,
        "step_count": len(steps),
        # BOTH NUMBERS, ALWAYS. A stale-cadence alarm reads the first; the second is what it must
        # not count, named, so "stale 0" can be told apart from "stale 0 because a step is off".
        "stale_step_count": sum(row["success_status"] == "stale" for row in steps),
        "retired_step_count": len(retired),
        "retired_steps": retired,
        "failed_step_count": len(failed),
        "backoff_step_count": sum(row["retry_state"] == "backoff" for row in failed),
        "ready_to_retry_count": sum(row["retry_state"] == "ready_to_retry" for row in failed),
        "steps": steps,
        # Compatibility fields retained while callers migrate to the all-step rows.
        "durability_sweep_stamp": durability.get("success_path"),
        "durability_sweep_stamp_status": durability.get("success_status"),
        "durability_sweep_stamp_age_s": durability.get("success_age_s"),
        "durability_sweep_stale_after_s": stale_after_seconds(durability),
    }


def shell_functions(registry: tuple[dict[str, Any], ...] | None = None) -> str:
    """Emit constant-only Bash functions consumed by orchestrate.sh.

    `cadence_retired KEY` succeeds, printing the row's `retirement_line`, when KEY is declared
    retired and none of its re-enable flags holds; otherwise it fails silently. `_cadence_due` is
    its only caller, so a retirement is honoured wherever a step's due-check is.
    """
    stamp_cases = []
    day_cases = []
    known_cases = []
    retired_cases = []
    for row in registry or CADENCE_STEPS:
        key = row["key"]
        stamp = row.get("success_stamp") or ""
        stamp_cases.append(f"    {key}) printf '%s\\n' '{stamp}' ;;")
        day_cases.append(f"    {key}) printf '%s\\n' '{int(row.get('cadence_days') or 0)}' ;;")
        known_cases.append(f"    {key}) return 0 ;;")
        held = retirement(row)
        if held is not None:
            lifted = " || ".join(
                f"[[ \"${{{name}:-}}\" == '{value}' ]]"
                for name, value in held["re_enable_env"].items()
            )
            retired_cases.extend(
                [
                    f"    {key})",
                    f"      if {lifted}; then return 1; fi",
                    f"      printf '%s\\n' {shlex.quote(str(retirement_line(row)))} ;;",
                ]
            )
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
            'cadence_retired() { case "$1" in',
            *retired_cases,
            "    *) return 1 ;;",
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
