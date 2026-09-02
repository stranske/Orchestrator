#!/usr/bin/env python3
"""Executable capability activation lifecycle.

The feature registry describes reusable code. This ledger records the separate,
stronger claim that a capability is matched, invoked, consumed, and connected to
an outcome. Mutable state lives on local disk so launchd and the canonical source
tree share one truth.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import feedback

ORCH = Path(__file__).resolve().parent
LOCAL_RUNTIME = Path(os.environ.get("ORCH_LOCAL_RUNTIME", Path.home() / ".codex" / "orchestrator"))
REG = Path(os.environ.get("ORCH_CAPABILITIES_PATH", LOCAL_RUNTIME / "capabilities.json"))
FEATURES_REG = Path(os.environ.get("ORCH_FEATURES_PATH", ORCH / "experiments" / "features.json"))
SCHEMA_VERSION = 1
GATED_TTL_DAYS = 90
# Kinds the COMPILER emits and `capability_targets` can bind/roll back at runtime. This set is a
# contract with capability_targets.TARGET_KINDS and must stay identical to it.
COMPILE_TARGET_KINDS = ("role", "workflow", "skill", "playbook", "gate")
# Kinds that exist in the LEDGER but have no runtime binding, because nothing compiles them.
# "module" (added 2026-08-09) covers hand-written lanes that were MIGRATED into the ledger —
# testgen_lane.py, adversarial.py, capacity.py/router.py. They have no compiled artifact, which is
# why 0 of 33 capabilities carried version lineage and routing was permanently unreachable; their
# artifact is their real source (see adopt_module_version). Naming the kind honestly is better than
# forcing them into a compiled kind they are not — and a `module` must never be handed to
# capability_targets, which has nothing to bind.
ADOPTION_ONLY_KINDS = ("module",)
TARGET_KINDS = COMPILE_TARGET_KINDS + ADOPTION_ONLY_KINDS
CAPABILITY_POLICY_VERSION = "capability-lifecycle/v2"
DEFAULT_PROMOTION_POLICY = {
    "min_independent_durable_reuse": 3,
    "max_failures": 0,
    "max_rework": 0,
    "max_evidence_age_days": 30,
}

CANONICAL_STATES = (
    "observed",
    "clustered",
    "generated",
    "validated",
    "wired",
    "shadow",
    "exercised",
    "canary",
    "active",
    "retired",
    "superseded",
)

TRANSITIONS = {
    "observed": {"clustered", "retired", "superseded"},
    "clustered": {"generated", "retired", "superseded"},
    "generated": {"validated", "retired", "superseded"},
    "validated": {"wired", "retired", "superseded"},
    "wired": {"shadow", "retired", "superseded"},
    "shadow": {"exercised", "canary", "retired", "superseded"},
    "exercised": {"canary", "retired", "superseded"},
    "canary": {"active", "shadow", "retired", "superseded"},
    "active": {"canary", "shadow", "retired", "superseded"},
    "retired": {"observed", "superseded"},
    "superseded": set(),
}

REQUIRED_FIELDS = (
    "schema_version",
    "capability_id",
    "status",
    "owner",
    "matcher",
    "entrypoint",
    "trigger_cadence",
    "flags_defaults",
    "output_artifact",
    "downstream_consumer",
    "learning_sink",
    "activation_evidence",
    "last_match",
    "last_invocation",
    "last_success",
    "outcome_links",
    "gate_reason",
    "gate_evidence",
    "evidence_threshold",
    "activation_deadline",
    "expiry",
    "next_transition",
    "kill_switch",
    "rollback",
    "predecessor",
    "successor",
    "event_history",
)

ACTIVE_PROBES = (
    "producer_probe",
    "consumer_probe",
    "outcome_probe",
    "rollback_probe",
)

EVENT_FIELDS = {
    "match": "last_match",
    "invocation": "last_invocation",
    "success": "last_success",
    "output": None,
    "consumer": None,
    "failure": None,
    "outcome": None,
    # AN AMENDMENT TO AN EARLIER OUTCOME, not an outcome of its own. A DISTINCT TYPE on purpose:
    # `capability_propensity.experiments()` dispatches on `match` / `invocation` / `outcome`, so a
    # reader that predates this type falls through every branch and ignores the event entirely —
    # which means an unsynced mirror or an older checkout reads the PRE-AMENDMENT truth instead of
    # misreading it. Tagging it `outcome` and telling the two apart by metadata was the first
    # attempt and it was unsafe in exactly one direction: an older reader computes
    # `useful if metadata["useful"] is True else not_useful`, and an amendment carries no `useful`
    # key, so every CORROBORATION would have been read as a REFUTATION.
    "outcome_amendment": None,
    # A SECOND OFFER answering a recorded decline, carrying only declared facts the first offer
    # omitted. Distinct from `match` for the same forward-compatibility reason as
    # `outcome_amendment`: a reader that predates it has no branch for the type and ignores it, so
    # an unsynced mirror neither miscounts it as a fresh candidate (which would inflate the
    # denominator `DEMOTION_MIN_TRIALS` reads) nor mistakes it for a decline.
    "offer_amendment": None,
}

KNOWN_GATES: dict[str, dict[str, Any]] = {
    "research-usage-guard": {
        "findability_category": "exercise_bound",
        "findability_rationale": (
            "Deterministic admission control for research followups, invoked by exp_abcd's "
            "followup path. Admission control selected by the admitted is no control, so no "
            "agent surface may offer it; its read surface is the anomaly report it emits."
        ),
        "status": "wired",
        "entrypoint": "research_usage_guard.py:main",
        "matcher": {"kind": "tick_phase", "name": "research-usage-guard"},
        "trigger_cadence": "daily local report plus every optional-research admission",
        "flags_defaults": {
            "ORCH_RESEARCH_ARM": "0",
            "ORCH_RESEARCH_USAGE_BYPASS": "0",
        },
        "output_artifact": "research-usage-report.json and research_usage_opportunities rows",
        "downstream_consumer": "orchestrate.sh cadence health and operator capacity review",
        "learning_sink": "feedback research-usage opportunity ledger and observed review runs",
        "gate_reason": "optional research is fail-closed on missing provenance, anomaly, and budget",
        "gate_evidence": "pre-dispatch decision and terminal outcome share one opportunity id",
        "evidence_threshold": "seven days with zero missing-spec judge calls and budgets respected",
        "notes": "dedup: checked research scheduler, experiment followup, feedback telemetry, and "
        "capacity controls; no existing control bound immutable-input admission to a local budget",
    },
    "local-model-profile-trial": {
        # Findability, declared ON THE GATE ENTRY. A twin entry in the declarations
        # table is forbidden: reconciliation merges the two dicts and the twin CLOBBERS
        # the gate's own fields (measured: the canary status vanished from the summary
        # view the moment a twin existed).
        "findability_category": "exercise_bound",
        "findability_rationale": (
            "quarantine-only by policy: trial transport goes through model_profile_trial_bridge and "
            "the pinned read-only Workflows runner; offering it at an agent surface would invite the "
            "un-quarantined dispatch the policy forbids. "
        ),
        "status": "shadow",
        "entrypoint": "model_profile_trial.py",
        "matcher": {"kind": "supervised_trial", "name": "sol-terra-luna"},
        "trigger_cadence": "explicit read-only instrumentation trial",
        "flags_defaults": {
            "assignment": "instrumentation",
            "learning_enabled": False,
            "promotion_allowed": False,
        },
        "output_artifact": "model-profile-trial state with source-integrity proof",
        "downstream_consumer": "periodic_report.py and observability_dashboard.py",
        "learning_sink": "none; instrumentation attempts are excluded from profile learning",
        "gate_reason": "identity plumbing trial remains shadow-only and non-promoting",
        "gate_evidence": "one frozen packet, one shared-pool snapshot, and source manifests",
        "evidence_threshold": "productive accepted work with causal profile and durability joins",
    },
    "completion-event-lineage": {
        # Findability, declared ON THE GATE ENTRY. A twin entry in the declarations
        # table is forbidden: reconciliation merges the two dicts and the twin CLOBBERS
        # the gate's own fields (measured: the canary status vanished from the summary
        # view the moment a twin existed).
        "findability_category": "exercise_bound",
        "findability_rationale": (
            "feedback.record_completion_event is the Brain's lineage write path, invoked by the "
            "tick:learning pattern-miner cadence; no agent selects a lineage stamp. "
        ),
        "status": "canary",
        "entrypoint": "feedback.py:record_completion_event",
        "matcher": {"kind": "feedback_event", "name": "record_run"},
        "trigger_cadence": "every dispatch, attempt, outcome, and durability update",
        "flags_defaults": {"enabled": True, "max_event_bytes": 12288},
        "output_artifact": "feedback.completion_events + feedback.influence_edges",
        "downstream_consumer": "feedback.py:completion_event_episodes",
        "learning_sink": "feedback Brain completion-event tables",
        "gate_reason": "safe-envelope lineage is in a bounded canary before active promotion",
        "gate_evidence": "normal dispatch/outcome hooks and offline seven-phase fixtures are enabled",
        "evidence_threshold": "distinct durable subjects retain complete non-orphan lineage without redaction regressions",
    },
    "live-keepalive-supervisor": {
        # Findability, declared ON THE GATE ENTRY. A twin entry in the declarations
        # table is forbidden: reconciliation merges the two dicts and the twin CLOBBERS
        # the gate's own fields (measured: the canary status vanished from the summary
        # view the moment a twin existed).
        "findability_category": "exercise_bound",
        "findability_rationale": (
            "a staged planner over already-escalated keepalive PRs that refuses live action under the "
            "single-authority rule; runs as the keepalive-stage2-plan cadence step. "
        ),
        "status": "wired",
        "entrypoint": "keepalive_supervisor.py",
        # MATCHER CORRECTED 2026-08-21. It declared `evidence_gate/ready_for_supervised_apply`,
        # which is what this module REPORTS ON, not what makes it fire. What makes it fire is the
        # daily `keepalive-stage2-plan` cadence step, and `keepalive_supervisor.main`'s own parser
        # says "no live action": it plans and reports, and its heartbeat fires unconditionally at CLI
        # entry with ref="keepalive_supervisor.main". A report cannot merge a PR, so demanding a
        # delivery outcome from it was the observer category error — 11 invocations stuck in
        # `invoked_without_outcomes` with advice ("fix outcome linkage") describing work that does
        # not exist for it. `tick_phase` is already an observer kind, so the classification follows
        # from the corrected declaration with NO change to is_observer or classify_liveness.
        # Deliberately NOT done: adding `evidence_gate` to OBSERVER_MATCHER_KINDS. That kind is
        # shared with `redirect-apply-bootstrap`, which really does deliver (it applies a plan that
        # dispatches a run), so widening the set would hide a linkage gap that is genuinely expected
        # there. The kind does not carry the distinction; the cadence declaration does.
        "matcher": {"kind": "tick_phase", "name": "keepalive-stage2-plan"},
        "trigger_cadence": "daily shadow evidence",
        "flags_defaults": {"live_supervisor_allowed": False},
        "gate_reason": "supervised apply remains disabled",
        "gate_evidence": "live_supervisor_allowed=false",
        "evidence_threshold": "linked live role outcomes and disagreements satisfy the Stage 2 gate",
    },
    "thompson-hybrid-routing": {
        "status": "shadow",
        "entrypoint": "router.py",
        "matcher": {"kind": "env", "name": "ORCH_EXPLORATION_MODE", "equals": "thompson-hybrid"},
        "trigger_cadence": "per route decision",
        "flags_defaults": {"ORCH_EXPLORATION_MODE": "epsilon-greedy"},
        # router.py heartbeats this only when Thompson ACTUALLY chose a challenger. While the mode
        # is epsilon-greedy it never chooses, so it can influence no run and earn no outcome; the 2
        # recorded invocations are from a window when the mode was on. Forward tagging is in place
        # (dispatcher._exercised_capability_ids), so outcomes flow the moment the mode is selected.
        "gate_blocks_execution": True,
        "gate_reason": "epsilon-greedy remains the reviewed default",
        "gate_evidence": "ORCH_EXPLORATION_MODE=epsilon-greedy",
        "evidence_threshold": "exploration review recommends thompson-hybrid",
    },
    "strategy-experiments": {
        "status": "wired",
        "entrypoint": "strategy_experiment.py",
        "matcher": {"kind": "env", "name": "ORCH_STRATEGY_EXPERIMENT", "equals": "1"},
        "trigger_cadence": "supervised CLI",
        "flags_defaults": {"ORCH_STRATEGY_EXPERIMENT": "0"},
        "gate_reason": "strategy execution is supervised",
        "gate_evidence": "ORCH_STRATEGY_EXPERIMENT defaults off",
        "evidence_threshold": "causal arm identity and outcome attribution pass",
    },
    "synthesis-promotion": {
        # Findability, declared ON THE GATE ENTRY. A twin entry in the declarations
        # table is forbidden: reconciliation merges the two dicts and the twin CLOBBERS
        # the gate's own fields (measured: the canary status vanished from the summary
        # view the moment a twin existed).
        "findability_category": "exercise_bound",
        "findability_rationale": (
            "exp_abcd followup -> synthesis_promotion.reconcile, fired mechanically on "
            "experiment_phase=evaluated; offering it would invite a second delivery controller. "
        ),
        "status": "wired",
        "entrypoint": "exp_abcd.py:followup -> synthesis_promotion.py:reconcile",
        "matcher": {"kind": "experiment_phase", "equals": "evaluated"},
        "trigger_cadence": "bounded exp_abcd followup cadence",
        "flags_defaults": {
            "ORCH_FOLLOWUP_SHIP_GATE": "1",
            "direct_publication_allowed": False,
            "auto_merge_allowed": False,
        },
        "output_artifact": "experiment synthesis-promotion.json plus one verified delivery candidate",
        "downstream_consumer": "Workflows auto-pilot/Keepalive after explicit external delivery link",
        "learning_sink": "feedback synthesis outcome, influence edges, and source-arm completion lineage",
        "gate_reason": "candidate publication and merge remain outside the experiment harness",
        "gate_evidence": "candidate_ready requires synth_verified and direct publication is structurally false",
        "evidence_threshold": "one externally delivered candidate reaches a joined durable outcome without duplicate transitions",
    },
    "range-lane-rollout": {
        "status": "canary",
        "entrypoint": "range_lane_rollout.py",
        "matcher": {"kind": "env", "name": "ORCH_RANGE_LANE_ROLLOUT", "equals": "1"},
        # Its heartbeats fire ONLY on the live-apply branch of range_lane_rollout.main; the daily
        # PREVIEW never reaches them. So while the switch is off the delivering path cannot execute
        # and no outcome can exist — the 13 recorded invocations are from the trial window. Forward
        # tagging is in place (assignments carry capability_ids before dispatcher.run), so outcomes
        # flow the moment the switch is back on. SWITCH_ON_CRITERIA already names the condition.
        "gate_blocks_execution": True,
        "trigger_cadence": "daily preview",
        "flags_defaults": {
            "module_default": {"ORCH_RANGE_LANE_ROLLOUT": "0"},
            "orchestrate_default": {"ORCH_RANGE_LANE_ROLLOUT": "1"},
        },
        "gate_reason": "bounded live canary auto-reverts after its review date",
        "gate_evidence": "orchestrate.sh exports the flag only inside a dated, max-one-per-day trial",
        "evidence_threshold": "joined dispatch and durable outcome evidence supports extending the trial",
    },
    "runtime-ac-checks": {
        "status": "canary",
        "entrypoint": "runtime_ac_gate.py",
        "matcher": {"kind": "env", "name": "ORCH_RUN_RUNTIME_AC", "equals": "1"},
        "trigger_cadence": "required closer gate",
        "flags_defaults": {
            "orchestrate_default": {"ORCH_RUN_RUNTIME_AC": "1"},
            "command_execution_default": {"ORCH_RUNTIME_AC_ALLOW_COMMANDS": "0"},
        },
        "gate_reason": "advisory canary is limited to explicit runtime-AC items; command execution stays off",
        "gate_evidence": "required closer must carry a runtime-AC label or spec",
        "evidence_threshold": "joined gate artifacts and durable outcomes demonstrate useful discrimination",
    },
    "redirect-apply-bootstrap": {
        # Findability, declared ON THE GATE ENTRY. A twin entry in the declarations
        # table is forbidden: reconciliation merges the two dicts and the twin CLOBBERS
        # the gate's own fields (measured: the canary status vanished from the summary
        # view the moment a twin existed).
        "findability_category": "exercise_bound",
        "findability_rationale": (
            "the daily redirect apply/link cadence step, at most one authorised plan per day; an "
            "agent hand-applying would bypass the self-gating that defines it. "
        ),
        "status": "canary",
        "entrypoint": "redirect_apply.py:apply_candidates",
        "matcher": {"kind": "evidence_gate", "name": "redirect_apply_bootstrap_eligible"},
        "trigger_cadence": "daily",
        "flags_defaults": {"ORCH_REDIRECT_APPLY_BOOTSTRAP": "1"},
        "output_artifact": "redirect_apply corpus events (kind=redirect_apply) + outcome links",
        "downstream_consumer": "redirect_plan.py:apply_plan",
        "learning_sink": "feedback role runs and linked outcomes",
        "kill_switch": "ORCH_REDIRECT_APPLY_BOOTSTRAP=0",
        "rollback": {"transition": "retired"},
        "gate_reason": "armed 2026-08-21 by owner decision; bounded to one already-dead lane per "
        "day and self-disabling once the Stage-2 deficits close",
        "gate_evidence": "ORCH_REDIRECT_APPLY_BOOTSTRAP=1 in orchestrate.sh; apply refuses a live "
        "pid, a foreign claim, an un-stamped plan, a repeat target",
        "evidence_threshold": "synced_role_outcomes reaches LINKED_OUTCOME_TARGET and "
        "linked_disagreements reaches DISAGREEMENT_OUTCOME_TARGET, at which "
        "point bootstrap_needed goes false and the capability self-disables",
        "notes": "dedup 2026-08-21: searched by concept for apply/supervised/autonomous/"
        "confirm_target/auto_apply across the tree before writing anything. "
        "redirect_plan.apply_plan EXISTS and is complete (exact confirm_target, "
        "prompt-written-first, pid-dead skip, non-fatal claim release, abort on delegate "
        "failure) but had ZERO callers — the only reference was this ledger's own "
        "downstream_consumer string, so the record asserted a consumer that did not "
        "exist. redirect_shadow.summarize already computes ready_for_supervised_apply; "
        "keepalive_supervisor already computes the deficits and writes the per-target "
        "redirect reports. Nothing was rebuilt: this adds the missing CALLER over those "
        "reports plus the automatic outcome linker. It exists because the gate is a "
        "STRUCTURAL DEADLOCK — synced_role_outcomes counts only accepted/applied advice "
        "(join_role_to_outcome returns synced=False when accepted=False, and historical "
        "links are deliberately synced=False/not_role_learning=True), so the gate that "
        "authorises applying requires 10 applied outcomes. Measured 2026-08-21: 143 "
        "proposals, 119 valid, 124 historical replays (that route EXHAUSTED), "
        "synced_role_outcomes=5 and linked_disagreements=0, the 5 created by hand. The "
        "owner-review design produced 5 links in ~2 months, which CLAUDE.md §3 forbids "
        "as a per-item approval queue.",
    },
    "role-redirect": {
        "status": "shadow",
        "entrypoint": "roles.py:run_redirect_agent",
        "matcher": {"kind": "role", "equals": "redirect"},
        "trigger_cadence": "redirect sweep and supervised CLI",
        "flags_defaults": {"apply": False, "ORCH_ROLE_SHADOW": "1"},
        "output_artifact": "redirect plan and optional shadow-corpus row",
        "downstream_consumer": "redirect_plan.py:apply_plan",
        "learning_sink": "feedback role runs and linked outcomes",
        "gate_reason": "role proposes in shadow; deterministic rails retain apply authority",
        "gate_evidence": "roles.py refuses mutation and redirect_plan requires explicit apply confirmation",
        "evidence_threshold": "linked role outcomes demonstrate improvement over deterministic baseline",
    },
    "role-prompt": {
        "status": "wired",
        "entrypoint": "roles.py:run_prompt_agent",
        "matcher": {"kind": "role", "equals": "prompt"},
        "trigger_cadence": "underspecified or high-risk local dispatch; max once per cycle",
        "flags_defaults": {"dispatch": False, "ORCH_ROLE_SHADOW": "1"},
        "output_artifact": "validated dispatch prompt",
        "downstream_consumer": "dispatcher.py:plan_dispatch accepted-role lineage",
        "learning_sink": "feedback role runs and linked outcomes",
        "gate_reason": "bounded shadow authoring only; router/task type/gates remain deterministic",
        "gate_evidence": "selector diagnostics distinguish no match from capacity/gate withholding",
        "evidence_threshold": "five linked outcomes, three durable, and at least one rejected counterfactual",
    },
    "role-decomposer": {
        "status": "shadow",
        "entrypoint": "roles.py:run_decomposer_agent",
        "matcher": {"kind": "role", "equals": "decomposer"},
        "trigger_cadence": "task_type=epic or epic_lane dispatch; max once per cycle",
        "flags_defaults": {"dispatch": False, "ORCH_ROLE_SHADOW": "1"},
        "output_artifact": "validated subtask DAG and dispatch prompts",
        "downstream_consumer": "dispatcher prompt after epic_lane.py deterministic DAG validation",
        "learning_sink": "feedback role runs and linked outcomes",
        "gate_reason": "role produces plans only; subtasks remain behind deterministic rails",
        "gate_evidence": "roles.py does not dispatch generated subtasks",
        "evidence_threshold": "linked decomposition outcomes beat baseline task delivery",
    },
    "role-triage": {
        "status": "shadow",
        "entrypoint": "roles.py:run_triage_agent",
        "matcher": {"kind": "role", "equals": "triage"},
        "trigger_cadence": "one bounded tick backlog snapshot; max once per cycle",
        "flags_defaults": {"dispatch": False, "ORCH_ROLE_SHADOW": "1"},
        "output_artifact": "validated ranked backlog proposal",
        "downstream_consumer": "tick.py comparison only; deterministic order/router unchanged",
        "learning_sink": "feedback role runs and linked outcomes",
        "gate_reason": "proposal is advisory and cannot reorder, claim, or label work",
        "gate_evidence": "tick records agreement/disagreement without changing deterministic routing",
        "evidence_threshold": "five linked outcomes, three durable, and at least one disagreement",
    },
    "role-adjudicator": {
        "status": "shadow",
        "entrypoint": "roles.py:run_adjudicator_agent",
        "matcher": {"kind": "role", "equals": "adjudicator"},
        "trigger_cadence": "persisted runtime-AC versus adversarial/review disagreement only",
        "flags_defaults": {"dispatch": False, "ORCH_ROLE_SHADOW": "1"},
        "output_artifact": "validated blocker adjudication",
        "downstream_consumer": "tick.py advisory evidence; deterministic blocker policy unchanged",
        "learning_sink": "feedback role runs and linked outcomes",
        "gate_reason": "adjudication remains advisory",
        "gate_evidence": "role cannot directly merge, close, or relabel",
        "evidence_threshold": "linked adjudications demonstrate calibrated value",
    },
}


def _now() -> int:
    return int(time.time())


def _blank_capability(capability_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "capability_id": capability_id,
        "status": "observed",
        "owner": None,
        "matcher": None,
        "entrypoint": None,
        "trigger_cadence": None,
        "flags_defaults": {},
        "output_artifact": None,
        "downstream_consumer": None,
        "learning_sink": None,
        "activation_evidence": {},
        "last_match": None,
        "last_invocation": None,
        "last_success": None,
        "outcome_links": [],
        "gate_reason": None,
        "gate_evidence": None,
        "evidence_threshold": None,
        "activation_deadline": None,
        "expiry": None,
        "next_transition": None,
        "kill_switch": None,
        "rollback": None,
        "predecessor": None,
        "successor": None,
        "target_kind": None,
        "capability_version_id": None,
        "artifact_hash": None,
        "lifecycle_policy_hash": None,
        "lifecycle_policy": {},
        "causal_evidence": {},
        "routing_prior": {"alpha": 1.0, "beta": 1.0, "observations": 0},
        "rollback_pending": None,
        "event_history": [],
    }


def _stable_hash(namespace: str, value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )
    return "sha256:" + hashlib.sha256(namespace.encode() + b"\0" + encoded).hexdigest()


def _transition_in_place(
    cap: dict[str, Any], new_state: str, *, reason: str, timestamp: int, evidence_ref: str
) -> None:
    old_state = cap["status"]
    if new_state == old_state:
        return
    if new_state not in TRANSITIONS.get(old_state, set()):
        raise ValueError(f"illegal capability transition: {old_state} -> {new_state}")
    cap["status"] = new_state
    cap.setdefault("event_history", []).append(
        {
            "timestamp": timestamp,
            "type": "transition",
            "from": old_state,
            "to": new_state,
            "reason": reason,
            "evidence_refs": [evidence_ref],
        }
    )


def compiled_version_identity(
    capability_id: str,
    *,
    artifact: dict[str, Any],
    lifecycle_policy: dict[str, Any],
) -> dict[str, Any]:
    """Return the one immutable identity shared by ledger and target adapters."""
    policy = {**DEFAULT_PROMOTION_POLICY, **dict(lifecycle_policy or {})}
    policy["policy_version"] = CAPABILITY_POLICY_VERSION
    artifact_hash = _stable_hash("capability-artifact", artifact)
    policy_hash = _stable_hash("capability-lifecycle-policy", policy)
    version_id = (
        "capability-version:"
        + _stable_hash(
            "capability-version",
            {"capability_id": capability_id, "artifact_hash": artifact_hash},
        ).split(":", 1)[1][:32]
    )
    return {
        "capability_id": capability_id,
        "capability_version_id": version_id,
        "artifact_hash": artifact_hash,
        "lifecycle_policy_hash": policy_hash,
        "lifecycle_policy": policy,
    }


def resolve_entrypoint_sources(entrypoint: str, *, root: Path | None = None) -> list[Path]:
    """Real source files behind an entrypoint declaration, or [] when none resolve.

    Handles the forms actually present in the ledger: `exp_abcd.py`, `adapters.py/dispatcher.py`,
    `dispatcher.offload`, `capability_compiler.py:run_reference_workflow`. Returns [] rather than
    guessing when a path does not exist — an artifact hash over a file we cannot read would be a
    fabrication, and the whole point of the hash is that it is the real content.
    """
    base = Path(root) if root else ORCH
    found: list[Path] = []
    for part in str(entrypoint or "").split("/"):
        part = part.split(":", 1)[0].strip()
        if not part:
            continue
        if not part.endswith(".py") and "." in part:
            part = part.split(".", 1)[0] + ".py"  # `dispatcher.offload` -> dispatcher.py
        if not part.endswith(".py"):
            continue
        candidate = base / part
        if candidate.is_file() and candidate not in found:
            found.append(candidate)
    return found


def adopt_module_version(
    capability_id: str,
    *,
    path: Path = REG,
    root: Path | None = None,
    lifecycle_policy: dict[str, Any] | None = None,
    timestamp: int | None = None,
) -> dict[str, Any]:
    """Establish immutable version lineage for a capability MIGRATED into the ledger.

    `register_compiled_version` cannot do this: it refuses any existing capability whose immutables
    differ from the proposal, and every migrated record holds (None, None, None), so every attempt
    raised. That left routing permanently unreachable — `capability_routing_decision` requires
    lineage, so 0 of 33 capabilities could ever be eligible no matter how much evidence accrued.

    Adoption is deliberately NOT a way around immutability:
      * lineage is established only when currently UNSET — going from nothing to something is not
        changing an immutable;
      * the artifact is the sha256 of the capability's REAL entrypoint source, so a code change
        produces a different version id, and evidence stays attached to the version that earned it;
      * if lineage is already set and the source has since changed, this REPORTS drift and changes
        nothing — resolving that is a new-version decision, not a silent rewrite;
      * an unresolvable entrypoint refuses adoption rather than hashing a placeholder.

    Adoption does not activate anything. Routing also requires status=='active', which still has to
    be earned through the normal evidence path — this removes the structural blocker, not the gate.
    """
    now = _now() if timestamp is None else int(timestamp)
    with _locked(path):
        capabilities = _read_ledger_unlocked(path)["capabilities"]
        cap = capabilities.get(capability_id)
        if cap is None:
            raise ValueError(f"unknown capability: {capability_id}")
        sources = resolve_entrypoint_sources(cap.get("entrypoint") or "", root=root)
        if not sources:
            return {
                "capability_id": capability_id,
                "adopted": False,
                "reason": f"entrypoint does not resolve to readable source: {cap.get('entrypoint')!r}",
            }
        artifact = {
            "target_kind": "module",
            "entrypoint": cap.get("entrypoint"),
            "sources": [
                {"path": str(src.name), "sha256": hashlib.sha256(src.read_bytes()).hexdigest()}
                for src in sources
            ],
        }
        identity = compiled_version_identity(
            capability_id,
            artifact=artifact,
            lifecycle_policy=lifecycle_policy or cap.get("lifecycle_policy") or {},
        )
        current = (
            cap.get("capability_version_id"),
            cap.get("artifact_hash"),
            cap.get("lifecycle_policy_hash"),
        )
        proposed = (
            identity["capability_version_id"],
            identity["artifact_hash"],
            identity["lifecycle_policy_hash"],
        )
        if any(current):
            if current == proposed:
                return {
                    "capability_id": capability_id,
                    "adopted": False,
                    "reason": "already current",
                    "capability_version_id": current[0],
                }
            return {
                "capability_id": capability_id,
                "adopted": False,
                "reason": "version_drift",
                "detail": "source changed since lineage was established; this is a new-version "
                "decision, not an in-place rewrite",
                "current_version_id": current[0],
                "source_version_id": proposed[0],
            }
        cap["target_kind"] = "module"
        cap["capability_version_id"] = identity["capability_version_id"]
        cap["artifact_hash"] = identity["artifact_hash"]
        cap["lifecycle_policy_hash"] = identity["lifecycle_policy_hash"]
        cap["lifecycle_policy"] = identity["lifecycle_policy"]
        cap.setdefault("event_history", []).append(
            {
                "timestamp": now,
                "type": "module_version_adopted",
                "capability_version_id": identity["capability_version_id"],
                "sources": [s["path"] for s in artifact["sources"]],
            }
        )
        validate_capability(cap, now=now)
        _write_ledger_unlocked(path, capabilities)
        return {
            "capability_id": capability_id,
            "adopted": True,
            "capability_version_id": identity["capability_version_id"],
            "sources": [s["path"] for s in artifact["sources"]],
        }


def adopt_all_module_versions(*, path: Path = REG, root: Path | None = None) -> dict[str, Any]:
    """Adopt lineage for every capability that lacks it. Idempotent; reports refusals by reason."""
    results = [
        adopt_module_version(cid, path=path, root=root)
        for cid in sorted(_read_ledger_unlocked(path)["capabilities"])
    ]
    return {
        "adopted": [r["capability_id"] for r in results if r.get("adopted")],
        "skipped": {r["capability_id"]: r.get("reason") for r in results if not r.get("adopted")},
        "results": results,
    }


def register_compiled_version(
    capability_id: str,
    *,
    target_kind: str,
    artifact: dict[str, Any],
    lifecycle_policy: dict[str, Any],
    record: dict[str, Any],
    path: Path = REG,
) -> dict[str, Any]:
    """Register one immutable compiled version without profile/provider identity."""
    if target_kind not in TARGET_KINDS:
        raise ValueError(f"unsupported capability target_kind: {target_kind}")
    identity = compiled_version_identity(
        capability_id, artifact=artifact, lifecycle_policy=lifecycle_policy
    )
    artifact_hash = identity["artifact_hash"]
    policy = identity["lifecycle_policy"]
    policy_hash = identity["lifecycle_policy_hash"]
    version_id = identity["capability_version_id"]
    with _locked(path):
        capabilities = _read_ledger_unlocked(path)["capabilities"] if path.exists() else {}
        existing = capabilities.get(capability_id)
        if existing:
            immutable = (
                existing.get("capability_version_id"),
                existing.get("artifact_hash"),
                existing.get("lifecycle_policy_hash"),
            )
            proposed = (version_id, artifact_hash, policy_hash)
            if immutable != proposed:
                raise ValueError(f"immutable compiled capability changed: {capability_id}")
            return json.loads(json.dumps(existing))
        cap = {
            **_blank_capability(capability_id),
            **record,
            "capability_id": capability_id,
            "target_kind": target_kind,
            "capability_version_id": version_id,
            "artifact_hash": artifact_hash,
            "lifecycle_policy_hash": policy_hash,
            "lifecycle_policy": policy,
        }
        cap.setdefault("event_history", []).append(
            {
                "timestamp": _now(),
                "type": "compiled_version_registered",
                "capability_version_id": version_id,
                "artifact_hash": artifact_hash,
                "lifecycle_policy_hash": policy_hash,
            }
        )
        validate_capability(cap)
        capabilities[capability_id] = cap
        _write_ledger_unlocked(path, capabilities)
        return json.loads(json.dumps(cap))


def _causal_readiness(cap: dict[str, Any], rows: list[dict], now: int) -> dict[str, Any]:
    policy = {**DEFAULT_PROMOTION_POLICY, **(cap.get("lifecycle_policy") or {})}
    accepted = [row for row in rows if row["accepted_consumption"]]
    durable_subjects = sorted({row["subject_id"] for row in accepted if row["durable_success"]})
    terminal = [row for row in accepted if row["terminal_outcome"]]
    # Count from feedback's tally_class — the single classification both this readiness view and
    # reconcile_causal_lifecycle's routing prior consume, so the gate's number and the prior's
    # number cannot drift apart. "Every terminal non-success is a failure" was the defect this
    # replaced: lifecycle churn nothing ever adjudicated trained as incapability (§2).
    failures = sum(1 for row in terminal if row.get("tally_class") == "failure")
    churn_unattributed = sum(
        1 for row in terminal if row.get("tally_class") == "churn_unattributed"
    )
    advisory_self_edges = sum(1 for row in accepted if row.get("tally_class") == "advisory_self")
    rework = sum(1 for row in terminal if row["rework"])
    latest = max((int(row["observed_ts"] or 0) for row in terminal), default=0)
    max_age = int(policy["max_evidence_age_days"]) * 86400
    criteria = {
        "independent_durable_reuse": len(durable_subjects)
        >= int(policy["min_independent_durable_reuse"]),
        "failures": failures <= int(policy["max_failures"]),
        "rework": rework <= int(policy["max_rework"]),
        "evidence_age": bool(latest and now - latest <= max_age),
    }
    return {
        "ready": all(criteria.values()),
        "criteria": criteria,
        "durable_subjects": durable_subjects,
        "accepted_consumptions": len(accepted),
        "terminal_outcomes": len(terminal),
        "failures": failures,
        # Excluded-but-VISIBLE: a gate that cannot say what it declined to count is the silent
        # no-op class again. These two numbers are what the old "failures" over-counted.
        "churn_unattributed": churn_unattributed,
        "advisory_self_edges": advisory_self_edges,
        "rework": rework,
        "latest_evidence_ts": latest,
    }


def _matches_trigger(cap: dict[str, Any], trigger: dict[str, Any]) -> tuple[bool, list[str]]:
    """Does this capability's declared matcher select this trigger?

    FAILS CLOSED (fixed 2026-08-09). The previous version looped over matcher keys and only
    recorded a mismatch for the six keys it recognised, so any OTHER shape — `{"kind": "role", ...}`,
    `{"field": "task_type", ...}`, an absent matcher entirely — produced no reasons and therefore
    "matched". Nine of fourteen declared matchers used those shapes, so they matched EVERY trigger.
    That was inert in production only because `capability_routing_decision` separately requires
    status=='active' and immutable lineage, neither of which any capability has yet; the moment one
    did, capabilities would have been credited with work they never touched.

    Every path now returns an explicit reason, and a shape this function cannot evaluate is a
    NON-match rather than a silent pass — the same rule applied everywhere else in this file:
    silence must never read as a pass.
    """
    matcher = cap.get("matcher") or {}
    if not matcher:
        # Nothing declared means nothing can route here. Reporting that as "matches everything"
        # is what made unwired capabilities look universally eligible.
        return False, ["no_matcher_declared"]

    trigger_values = {
        "repo": str(trigger.get("repository") or ""),
        "repository": str(trigger.get("repository") or ""),
        "task_type": str(trigger.get("task_type") or ""),
        "task_types": str(trigger.get("task_type") or ""),
        "lane": str(trigger.get("lane") or ""),
        "lanes": str(trigger.get("lane") or ""),
        "role": str(trigger.get("role") or ""),
    }
    reasons: list[str] = []

    # Shape A: {"field": <trigger field>, "operator": "in", "value": [...]} — the only shape that
    # can be replayed against recorded run history, and the one used by work-routed lanes.
    if "field" in matcher:
        field = str(matcher.get("field") or "")
        values = matcher.get("value")
        values = values if isinstance(values, list) else [values]
        actual = trigger_values.get(field)
        if actual is None:
            return False, [f"unknown_matcher_field:{field}"]
        if actual not in {str(v) for v in values}:
            return False, [f"{field}_mismatch"]
        return True, []

    # Shape B: {"kind": <k>, "equals"|"name": <expected>} — a typed trigger.
    #
    # GENERALISED 2026-08-09. This used to understand only `env` and `role` and refuse everything
    # else by name, which left four kinds (feedback_event, evidence_gate, supervised_trial,
    # experiment_phase) permanently unmatched — one capability stranded behind each. Rather than
    # add a branch per kind, a kind now matches against a SAME-NAMED field the caller supplies in
    # the trigger. Adding a new trigger kind is then a caller-side change, not an edit here.
    #
    # Still fails closed: a trigger that does not carry the field is a NON-match with a named
    # reason, never a silent pass. `env` keeps its own branch because it is the one kind whose
    # value lives in the process environment rather than in the trigger, and whose `name` is the
    # variable rather than the expected value.
    if "kind" in matcher:
        kind = str(matcher.get("kind") or "").lower()
        if not kind:
            return False, ["empty_matcher_kind"]
        if kind == "env":
            name, want = str(matcher.get("name") or ""), str(matcher.get("equals"))
            actual = os.environ.get(name)
            return (actual == want), ([] if actual == want else [f"env_mismatch:{name}"])
        expected = matcher.get("equals", matcher.get("name"))
        if expected is None:
            return False, [f"matcher_kind_missing_expected_value:{kind}"]
        actual = trigger.get(kind) if kind not in trigger_values else trigger_values[kind]
        if actual in (None, ""):
            # The orchestrator did not supply this context, so we cannot claim a match.
            return False, [f"{kind}_not_in_trigger"]
        return (str(actual) == str(expected)), (
            [] if str(actual) == str(expected) else [f"{kind}_mismatch"]
        )

    # Shape C (legacy): bare {key: value} over the recognised trigger fields.
    for key, expected in matcher.items():
        if key not in trigger_values:
            return False, [f"unknown_matcher_key:{key}"]
        values = expected if isinstance(expected, list) else [expected]
        if trigger_values[key] not in {str(value) for value in values}:
            reasons.append(f"{key}_mismatch")
    return not reasons, reasons


def capability_routing_decision(
    trigger: dict[str, Any],
    *,
    capabilities_by_id: dict[str, dict[str, Any]],
    seed: int,
) -> dict[str, Any]:
    """Select active versions deterministically, independent of execution profile."""
    eligible, rejected = [], {}
    for capability_id, cap in sorted(capabilities_by_id.items()):
        matched, reasons = _matches_trigger(cap, trigger)
        if not matched:
            rejected[capability_id] = reasons
            continue
        if cap.get("rollback_pending"):
            rejected[capability_id] = ["rollback_pending"]
            continue
        if cap.get("status") != "active":
            rejected[capability_id] = [f"status:{cap.get('status')}"]
            continue
        if not all(
            cap.get(field)
            for field in ("capability_version_id", "artifact_hash", "lifecycle_policy_hash")
        ):
            rejected[capability_id] = ["immutable_lineage_missing"]
            continue
        eligible.append(cap)
    eligible.sort(
        key=lambda cap: (
            -float((cap.get("routing_prior") or {}).get("posterior") or 0.0),
            cap["capability_id"],
            cap["capability_version_id"],
        )
    )
    selected = eligible[0] if eligible else None
    body = {
        "policy_version": CAPABILITY_POLICY_VERSION,
        "seed": int(seed),
        "eligible_capability_ids": [cap["capability_id"] for cap in eligible],
        "eligible_capability_version_ids": [cap["capability_version_id"] for cap in eligible],
        "rejection_reasons": rejected,
        "selected_capability_id": selected["capability_id"] if selected else None,
        "selected_capability_version_id": (selected["capability_version_id"] if selected else None),
        "propensity": 1.0 if selected else 0.0,
        "fallback": None if selected else "baseline",
    }
    body["decision_id"] = (
        "capability-decision:"
        + _stable_hash("capability-routing-decision", {"trigger": trigger, **body}).split(":", 1)[
            1
        ][:24]
    )
    return body


def reconcile_causal_lifecycle(
    capability_id: str,
    *,
    path: Path = REG,
    timestamp: int | None = None,
) -> dict[str, Any]:
    """Reconcile readiness and priors from exact Brain joins, idempotently.

    A regression marks rollback pending and makes the version ineligible for
    routing; it does not pretend a target-specific rollback has executed.
    """
    now = _now() if timestamp is None else int(timestamp)
    with _locked(path):
        capabilities = _read_ledger_unlocked(path)["capabilities"]
        cap = capabilities.get(capability_id)
        if cap is None:
            raise ValueError(f"unknown capability: {capability_id}")
        version_id = str(cap.get("capability_version_id") or "")
        if not version_id or not cap.get("artifact_hash") or not cap.get("lifecycle_policy_hash"):
            raise ValueError(f"capability lacks immutable version lineage: {capability_id}")
        rows = feedback.capability_causal_evidence(capability_id, version_id)
        readiness = _causal_readiness(cap, rows, now)
        accepted = [row for row in rows if row["accepted_consumption"]]
        terminal = [row for row in accepted if row["terminal_outcome"]]
        successes = sum(1 for row in terminal if row.get("tally_class") == "success")
        failures = sum(1 for row in terminal if row.get("tally_class") == "failure")
        evidence_hash = _stable_hash(
            "capability-causal-evidence",
            [
                {
                    "edge_id": row["edge_id"],
                    "subject_id": row["subject_id"],
                    "target_run_id": row["target_run_id"],
                    "accepted_consumption": row["accepted_consumption"],
                    "outcome_verdict": row["outcome_verdict"],
                    "durability": row["durability"],
                    "profile_attempt_ids": row["profile_attempt_ids"],
                }
                for row in rows
            ],
        )
        previous_hash = (cap.get("causal_evidence") or {}).get("evidence_hash")
        cap["causal_evidence"] = {
            "evidence_hash": evidence_hash,
            "row_count": len(rows),
            "readiness": readiness,
            "reconciled_at": now,
        }
        tallied = successes + failures
        cap["routing_prior"] = {
            "alpha": 1.0 + successes,
            "beta": 1.0 + failures,
            "observations": tallied,
            "posterior": (1.0 + successes) / (2.0 + tallied),
            "evidence_hash": evidence_hash,
        }
        if rows:
            cap["last_match"] = max(int(row["observed_ts"] or 0) for row in rows)
        if accepted:
            cap["last_invocation"] = max(int(row["observed_ts"] or 0) for row in accepted)
            cap.setdefault("activation_evidence", {})["consumer_probe"] = {
                "passed": True,
                "checked_at": now,
                "ref": accepted[-1]["target_event_id"],
            }
            cap.setdefault("activation_evidence", {})["producer_probe"] = {
                "passed": True,
                "checked_at": now,
                "ref": accepted[-1]["source_event_id"],
            }
        durable = [row for row in terminal if row["durable_success"]]
        if durable:
            cap["last_success"] = max(int(row["observed_ts"] or 0) for row in durable)
            cap.setdefault("activation_evidence", {})["outcome_probe"] = {
                "passed": True,
                "checked_at": now,
                "ref": durable[-1]["target_run_id"],
            }
        cap["outcome_links"] = sorted({row["target_run_id"] for row in terminal})
        regressions = [row for row in terminal if row["regression"]]
        if regressions:
            cap["rollback_pending"] = {
                "reason": "joined capability outcome regressed",
                "evidence_ref": regressions[-1]["edge_id"],
                "detected_at": now,
                "predecessor": cap.get("predecessor"),
            }
        elif (cap.get("rollback_pending") or {}).get(
            "reason"
        ) == "joined capability outcome regressed":
            # The same review that SETS this flag clears it when its own evidence class
            # drains. Without this branch it is a one-way latch: the only other clear path
            # (execute_capability_rollback) requires a routable predecessor, and the flag
            # rejects the capability from routing decisions and derates it in the advisor
            # while it stands — measured on role-triage, whose pending was set by 96
            # unmerged-churn rows later reclassified as churn_unattributed. Cleared WITH a
            # named event, never silently, and only for the reason THIS function writes —
            # a pending recorded by other machinery is not ours to drain.
            cap["rollback_pending"] = None
            cap.setdefault("event_history", []).append(
                {
                    "timestamp": now,
                    "type": "rollback_pending_cleared",
                    "reason": (
                        "regression evidence drained under attributable-failure " "reclassification"
                    ),
                    "evidence_hash": evidence_hash,
                }
            )
        if cap.get("predecessor"):
            cap.setdefault("activation_evidence", {})["rollback_probe"] = {
                "passed": True,
                "checked_at": now,
                "ref": cap["predecessor"],
            }
        if not regressions:
            if cap.get("status") == "wired" and terminal:
                _transition_in_place(
                    cap,
                    "shadow",
                    reason="version-exact producer/consumer edge observed",
                    timestamp=now,
                    evidence_ref=evidence_hash,
                )
            if cap.get("status") == "shadow" and terminal:
                _transition_in_place(
                    cap,
                    "exercised",
                    reason="joined consumed outcome observed",
                    timestamp=now,
                    evidence_ref=evidence_hash,
                )
            if cap.get("status") == "canary" and readiness["ready"]:
                _transition_in_place(
                    cap,
                    "active",
                    reason="causal promotion thresholds satisfied",
                    timestamp=now,
                    evidence_ref=evidence_hash,
                )
                cap["next_transition"] = None
        if previous_hash != evidence_hash:
            cap.setdefault("event_history", []).append(
                {
                    "timestamp": now,
                    "type": "causal_reconcile",
                    "evidence_hash": evidence_hash,
                    "observations": successes + failures,
                    "successes": successes,
                    "failures": failures,
                    # What the tally deliberately did NOT count, so a drained or churn-heavy
                    # window is legible in the event log rather than silently absorbed.
                    "churn_unattributed": readiness.get("churn_unattributed", 0),
                    "advisory_self_edges": readiness.get("advisory_self_edges", 0),
                    "status": cap.get("status"),
                }
            )
        validate_capability(cap, now=now)
        _write_ledger_unlocked(path, capabilities)
        return {
            "capability_id": capability_id,
            "capability_version_id": version_id,
            "status": cap["status"],
            "readiness": readiness,
            "routing_prior": cap["routing_prior"],
            "rollback_pending": cap.get("rollback_pending"),
            "changed": previous_hash != evidence_hash,
        }


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_ledger_unlocked(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported capability ledger schema: {payload.get('schema_version')}")
    if not isinstance(payload.get("capabilities"), dict):
        raise ValueError("capability ledger missing capabilities object")
    return payload


def _write_ledger_unlocked(path: Path, capabilities: dict[str, dict[str, Any]]) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _now(),
        "capabilities": capabilities,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def migrate_features_to_capabilities(
    features_path: Path = FEATURES_REG,
    caps_path: Path = REG,
    *,
    now: int | None = None,
) -> dict[str, Any]:
    """Conservatively migrate feature names without inferring activation."""
    ts = _now() if now is None else now
    features: dict[str, Any] = {}
    if features_path.exists():
        features = json.loads(features_path.read_text())
    capabilities: dict[str, dict[str, Any]] = {}
    for name, feature in features.items():
        cap = _blank_capability(name)
        cap["status"] = "generated" if feature.get("maturity") == "hardened" else "observed"
        cap["entrypoint"] = feature.get("module")
        cap["event_history"].append(
            {
                "timestamp": ts,
                "type": "migrated",
                "source": "experiments/features.json",
                "legacy_maturity": feature.get("maturity"),
                "activation_inferred": False,
            }
        )
        capabilities[name] = cap

    expiry = ts + GATED_TTL_DAYS * 86400
    for name, gate in KNOWN_GATES.items():
        cap = capabilities.setdefault(name, _blank_capability(name))
        cap.update(gate)
        cap["expiry"] = expiry
        cap["next_transition"] = "retired"
        cap["activation_deadline"] = expiry
        cap["kill_switch"] = "restore documented default-off gate"
        cap["rollback"] = {"transition": "retired", "reason": "gate evidence expired or regressed"}
        cap["event_history"].append(
            {
                "timestamp": ts,
                "type": "gate_registered",
                "source": "2026-07-08 dormancy inventory",
                "activation_inferred": False,
            }
        )

    with _locked(caps_path):
        if caps_path.exists():
            raise FileExistsError(f"refusing to overwrite existing capability ledger: {caps_path}")
        _write_ledger_unlocked(caps_path, capabilities)
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": ts,
        "capabilities": capabilities,
    }


def _expire_in_place(capabilities: dict[str, dict[str, Any]], now: int) -> list[str]:
    retired: list[str] = []
    for name, cap in capabilities.items():
        expiry = cap.get("expiry")
        if (
            cap.get("status") not in {"retired", "superseded"}
            and expiry is not None
            and now >= int(expiry)
        ):
            previous = cap["status"]
            cap["status"] = "retired"
            cap["next_transition"] = None
            cap.setdefault("event_history", []).append(
                {
                    "timestamp": now,
                    "type": "transition",
                    "from": previous,
                    "to": "retired",
                    "reason": "expiry reached; safe default",
                }
            )
            retired.append(name)
    return retired


# The fields a DECLARATION owns. Reconciliation rewrites these from code on every load, so a value
# typed into a live ledger cannot survive — which is the point: the declaration is reviewed in a diff
# or it is not reviewed at all. Everything else on a capability row is measured state (invocations,
# outcomes, event history) and is owned by the ledger, never overwritten from here.
#
# `kill_switch_category` / `control_point` / `kill_switch_rationale` joined on 2026-08-22. They had
# been applied straight to the running instance's ledger, which made them machine-local: green where
# someone typed them, absent on a fresh checkout, and invisible to review.
DECLARATION_FIELDS: tuple[str, ...] = (
    "entrypoint",
    "matcher",
    "trigger_cadence",
    "flags_defaults",
    "output_artifact",
    "downstream_consumer",
    "learning_sink",
    "gate_reason",
    "gate_evidence",
    "evidence_threshold",
    # Dedup-before-develop evidence is part of the reviewed declaration, not machine-local prose.
    "notes",
    "gate_blocks_execution",
    "kill_switch_category",
    "control_point",
    "kill_switch_rationale",
    # The findability exemption, carried here for the same reason as the kill-switch categories: a
    # declaration typed into the running instance's JSON is machine-local and invisible to review.
    # `capability_admission.req_findable` reads these two, and both are required together.
    "findability_category",
    "findability_rationale",
)


# Declaration-owned fields for capabilities that are NOT gates. `KNOWN_GATES` seeds gate machinery
# — a status, a GATED_TTL_DAYS expiry, `next_transition: retired` — so putting a non-gate capability
# there would assert an expiry it does not have. This table carries ONLY declaration-owned fields and
# is consumed by the SAME `_reconcile_known_declarations` loop: one mechanism, two sources, not a
# second inventory.
#
# WHY IT EXISTS. `kill_switch_category` / `control_point` / `kill_switch_rationale` were applied
# straight to the running instance's ledger, so they were machine-local: green where applied, absent
# on a fresh checkout, and invisible to code review. That is the same class as every other
# declaration this module reconciles, and the fix is the same — declare it in code and let
# reconciliation seed it. Adding a capability here means its declaration is reviewed with the diff
# rather than typed into a live JSON file nobody diffs.
KNOWN_DECLARATIONS: dict[str, dict[str, Any]] = {
    "agy-runtime-isolation": {
        "findability_category": "exercise_bound",
        "findability_rationale": (
            "adapters.build_command adds --add-dir on every gemini dispatch and the dispatcher tags it; this capability IS the confinement, and selection pressure must never reach a confinement property."
        ),
        "kill_switch_category": "safety_guard",
        "kill_switch_rationale": (
            "This capability IS the confinement: adapters.build_command adds an absolute "
            "`--add-dir <cwd>` so gemini writes only inside the target worktree (without it AGY "
            "falls back to an internal dir). Turning it OFF is strictly more dangerous than leaving "
            "it ON, so a kill switch would be an anti-feature rather than a control."
        ),
    },
    "windowed-capacity-policy": {
        "kill_switch_category": "compute_only",
        "control_point": "ORCH_DISABLE_STEPS",
        "kill_switch_rationale": (
            "Computes seat state; takes no action. Disabling it would not stop a dispatch — it would "
            "blind the router and make selection WORSE, the opposite of what a kill switch is for. "
            "The real control is at the consumers that act: the tick steps, each honouring "
            "ORCH_DISABLE_STEPS, plus ORCH_OFFLOAD_DISABLED on the transport."
        ),
    },
    "redirect-policy": {
        "kill_switch_category": "compute_only",
        "control_point": "ORCH_DISABLE_STEPS",
        "kill_switch_rationale": (
            "Produces an advisory wait/collect/inspect/redirect/decompose DECISION and nothing else. "
            "Every mutating step downstream is separately gated. The control is at the sweep that "
            "invokes it, ORCH_DISABLE_STEPS=redirect-sweep."
        ),
    },
    # THE FORMER FINDABILITY EXEMPTION, ENDED 2026-09-02. These rails are INVOKED by a rail rather
    # than OFFERED for their job, and their rationales still say so — that part is unchanged and
    # still true. What changed is the owner's directive that every capability gets a chance to be
    # exercised and every internal rail is offerable in code: a capability nobody can be offered can
    # never be triggered under a consult, so it can never earn a verdict, and fifteen rows sat in
    # the docket as "excluded and counted" for the whole campaign. `exercise_bound` means: bound on
    # a `rail-exercise:<phase>` surface (capability_advisor.SURFACE_BINDINGS), where the binding
    # reason IS the exercise — a read-only or dry-run run of the rail's own code against a fixture
    # or its own artifact, scored by a pre-committed check. The live path stays with the rail.
    # Declared here rather than in a live ledger for the same reason the kill-switch categories
    # moved: a machine-local declaration is green where it was typed, absent on a fresh checkout,
    # and invisible to review.
    "capability-admission-gate": {
        "findability_category": "exercise_bound",
        "findability_rationale": (
            "verify.py runs this gate on every PR unconditionally, as one of its five gates. No "
            "agent ever chooses it, so binding it to a surface could not change how often it runs; "
            "the thing that would change its reach is a CI-side consult, which does not exist."
        ),
    },
    "docs-drift-fix-agent": {
        "findability_category": "exercise_bound",
        "findability_rationale": (
            "its entrypoint is a Workflows GitHub Actions workflow that fires per PR in another "
            "repository; invocations arrive here through "
            "capability_outcome_bridge.ingest_external_ci_invocations. There is no local reasoning "
            "context to offer it to, so no surface can make it more findable."
        ),
    },
    # ELEVEN INTERNAL RAILS AND ONE QUARANTINED TRANSPORT, declared 2026-08-29 from the hard-tail
    # assessment (each rationale names the rail that invokes it). Declared HERE, not only in a live
    # ledger, because a declaration typed into one machine's capabilities.json is green where it
    # was typed, absent on a fresh checkout, and invisible in a diff — the drift guard in
    # test_capability_admission exists to force exactly this placement.
    "evidence-acquisition": {
        "findability_category": "exercise_bound",
        "findability_rationale": (
            "evidence_acquisition.py only OBEYS capabilities.unblock() — re-deriving the decision at "
            "a surface would create a second opinion; it runs as an orchestrate.sh shadow cadence "
            "step. "
        ),
    },
    "feature-reflection-cli": {
        "findability_category": "exercise_bound",
        "findability_rationale": (
            "its agent-facing half is already offered as feature-scan (same entrypoint module); this "
            "row is the features.py registry machinery fed by the daily tick step and read in "
            "periodic_report. "
        ),
    },
    "issue-readiness": {
        "findability_category": "exercise_bound",
        "findability_rationale": (
            "orchestrate.sh invokes issue_readiness.py on cadence, feeding backlog._is_ready via the "
            "status:ready label; agents consume its labels, and a manual invocation would race the "
            "cadence. "
        ),
    },
    "capability:reference-sync-hygiene-test-gate": {
        "findability_category": "exercise_bound",
        "findability_rationale": (
            "capability_compiler.run_reference_workflow is invoked only by "
            "consumer_sync_shadow.record_shadow_result via the consumer-sync ingest cadence; same "
            "shape as capability-admission-gate. "
        ),
    },
    "research-scheduler": {
        "findability_category": "exercise_bound",
        "findability_rationale": (
            "tick.research_tick consumes its planning functions behind ORCH_RESEARCH_ARM; an agent "
            "wanting an experiment invokes exp_abcd directly, not the scheduler. "
        ),
    },
    "feedback-store": {
        "findability_category": "exercise_bound",
        "findability_rationale": (
            "the learning store's spine: every dispatch, outcome and learning-cadence step writes it. Substrate cannot be task-selected."
        ),
        "kill_switch_category": "compute_only",
        "control_point": "ORCH_DISABLE_STEPS",
        "kill_switch_rationale": (
            "The Brain's append-only write path — it RECORDS what other capabilities did and takes "
            "no action of its own. A switch that stops it does not stop any work; it destroys the "
            "telemetry for work that happens anyway, which is strictly worse than the failure it "
            "would be reached for. Controls belong at the producers."
        ),
    },
}


def _reconcile_known_declarations(capabilities: dict[str, dict[str, Any]], now: int) -> bool:
    """Refresh contract fields when code declarations evolve.

    The ledger must preserve accumulated evidence, but leaving old matcher,
    gate, or consumer text in place makes a newly wired feature look dormant.
    Reconciliation therefore updates declaration-owned fields and records the
    change. It never invents match/invocation/outcome evidence and never
    downgrades an active, retired, or superseded capability.
    """
    changed = False
    declaration_fields = DECLARATION_FIELDS
    # BOTH SOURCES, one loop. KNOWN_GATES carries gate machinery; KNOWN_DECLARATIONS carries only
    # declaration-owned fields for capabilities that are not gates.
    for name, gate in {**KNOWN_GATES, **KNOWN_DECLARATIONS}.items():
        cap = capabilities.get(name)
        if cap is None:
            continue
        changed_fields = []
        for field in declaration_fields:
            if field in gate and cap.get(field) != gate[field]:
                cap[field] = json.loads(json.dumps(gate[field]))
                changed_fields.append(field)
        declared_status = gate.get("status")
        if (
            declared_status
            and cap.get("status") not in {"active", "retired", "superseded"}
            and cap.get("status") != declared_status
        ):
            previous = cap.get("status")
            cap["status"] = declared_status
            changed_fields.append("status")
            cap.setdefault("event_history", []).append(
                {
                    "timestamp": now,
                    "type": "declaration_reconciled",
                    "from": previous,
                    "to": declared_status,
                    "activation_inferred": False,
                }
            )
        if changed_fields:
            cap.setdefault("event_history", []).append(
                {
                    "timestamp": now,
                    "type": "declaration_reconciled",
                    "changed_fields": sorted(set(changed_fields)),
                    "activation_inferred": False,
                }
            )
            changed = True
    return changed


def load(path: Path = REG, *, create: bool = True) -> dict[str, dict[str, Any]]:
    with _locked(path):
        if not path.exists():
            if not create:
                return {}
            features: dict[str, Any] = {}
            if FEATURES_REG.exists():
                features = json.loads(FEATURES_REG.read_text())
            now = _now()
            capabilities: dict[str, dict[str, Any]] = {}
            for name, feature in features.items():
                cap = _blank_capability(name)
                cap["status"] = "generated" if feature.get("maturity") == "hardened" else "observed"
                cap["entrypoint"] = feature.get("module")
                cap["event_history"].append(
                    {"timestamp": now, "type": "migrated", "activation_inferred": False}
                )
                capabilities[name] = cap
            expiry = now + GATED_TTL_DAYS * 86400
            for name, gate in KNOWN_GATES.items():
                cap = capabilities.setdefault(name, _blank_capability(name))
                cap.update(gate)
                cap.update(
                    {
                        "expiry": expiry,
                        "activation_deadline": expiry,
                        "next_transition": "retired",
                        "kill_switch": "restore documented default-off gate",
                        "rollback": {
                            "transition": "retired",
                            "reason": "gate evidence expired or regressed",
                        },
                    }
                )
                cap["event_history"].append(
                    {"timestamp": now, "type": "gate_registered", "activation_inferred": False}
                )
            _write_ledger_unlocked(path, capabilities)
        ledger = _read_ledger_unlocked(path)
        capabilities = ledger["capabilities"]
        if not create:
            # RAW rows, exactly as they sit on disk: declaration-owned fields (matcher, status,
            # gate_reason, gate_blocks_execution, flags_defaults, ...) are NOT reconciled here.
            # If you are about to assert on OR REPORT one of those, use `load_declared` instead —
            # see the 2026-08-21 incidents recorded in its docstring (a test) and at `summary` (a
            # report; the same race, and there it decides a dry_seam_audit gate).
            return json.loads(json.dumps(capabilities))
        now = _now()
        declarations_added = False
        # Code upgrades may introduce a capability after the local ledger already
        # exists. Register missing declarations conservatively without overwriting
        # accumulated lifecycle state or evidence on existing records.
        for name, gate in KNOWN_GATES.items():
            if name in capabilities:
                continue
            cap = _blank_capability(name)
            cap.update(gate)
            expiry = now + GATED_TTL_DAYS * 86400
            cap.update(
                {
                    "expiry": expiry,
                    "activation_deadline": expiry,
                    "next_transition": "retired",
                    "kill_switch": "restore documented default-off gate",
                    "rollback": {
                        "transition": "retired",
                        "reason": "gate evidence expired or regressed",
                    },
                }
            )
            cap["event_history"].append(
                {
                    "timestamp": now,
                    "type": "gate_registered",
                    "source": "code declaration reconciliation",
                    "activation_inferred": False,
                }
            )
            capabilities[name] = cap
            declarations_added = True
        declarations_reconciled = _reconcile_known_declarations(capabilities, now)
        if _expire_in_place(capabilities, now) or declarations_added or declarations_reconciled:
            _write_ledger_unlocked(path, capabilities)
        return json.loads(json.dumps(capabilities))


def load_declared(path: Path = REG) -> dict[str, dict[str, Any]]:
    """The ledger as the system will see it AFTER declaration reconciliation — WITHOUT writing it.

    WHY THIS EXISTS. `load(create=False)` returns rows exactly as they sit on disk, but
    declaration-owned fields (`matcher`, `status`, `gate_reason`, `gate_blocks_execution`,
    `flags_defaults`, ...) are brought up to date only by a `load()` that RECONCILES — that is, by a
    different call, possibly in a different process. A `create=False` reader therefore lands on
    whichever side of that write it happens to race, and nothing in its result says which side it
    got.

    That is not hypothetical. On 2026-08-21 `test_evidence_gate_kind_is_not_blanket_observer` failed
    once inside a `verify.py` run and passed on every re-run: `live-keepalive-supervisor`'s matcher
    was still the old `{"kind": "evidence_gate"}` on disk, and reconciliation rewrote it to
    `tick_phase` at 08:07:28 — mid-suite, driven by an unrelated edit to this module's
    `KNOWN_GATES`. The row's own `declaration_reconciled` event records the write. Both exposed
    tests were the only live-ledger readers that passed `create=False`; every other one already used
    the reconciling `load()` and was immune.

    So: a reader that asserts on a declaration-owned field must reconcile FIRST, and a reader that
    must not mutate shared state — a test, a report — cannot use the writing `load()` to do it.
    This reconciles the in-memory copy and writes NOTHING. Reconciliation events land on the copy,
    exactly as the writing path would record them, and never reach disk.
    """
    capabilities = load(path, create=False)
    _reconcile_known_declarations(capabilities, _now())
    return capabilities


def save(capabilities: dict[str, dict[str, Any]], path: Path = REG) -> None:
    for capability_id, cap in capabilities.items():
        if cap.get("capability_id") != capability_id:
            raise AssertionError(
                f"capability key/id mismatch: {capability_id!r} != {cap.get('capability_id')!r}"
            )
        validate_capability(cap)
    with _locked(path):
        _write_ledger_unlocked(path, capabilities)


def validate_capability(cap: dict[str, Any], *, now: int | None = None) -> None:
    missing = [field for field in REQUIRED_FIELDS if field not in cap]
    if missing:
        raise AssertionError(f"capability missing required fields: {', '.join(missing)}")
    status = cap.get("status")
    if status not in CANONICAL_STATES:
        raise AssertionError(f"capability has invalid status: {status}")
    if cap.get("schema_version") != SCHEMA_VERSION:
        raise AssertionError("capability has unsupported schema_version")
    # `gate_criteria` feeds gate_policy(), which does dict(...) on it. A string passes every other
    # check here but raises inside `capabilities usage` / the daily cadence step, so a malformed
    # registration would break the cadence rather than the registration. Reject it at the door.
    criteria = cap.get("gate_criteria")
    if criteria is not None and not isinstance(criteria, dict):
        raise AssertionError(
            f"gate_criteria must be a dict of machine-checkable bounds "
            f"({sorted(GATE_BOUND_KEYS)} and/or 'requires'), got {type(criteria).__name__}; "
            f"put prose in gate_criteria_prose"
        )
    if cap.get("gate_reason") and status not in {"retired", "superseded"}:
        for field in ("gate_evidence", "evidence_threshold", "expiry", "next_transition"):
            if not cap.get(field):
                raise AssertionError(f"gated capability missing {field}")
    if status != "active":
        return
    required_active = (
        "owner",
        "matcher",
        "entrypoint",
        "trigger_cadence",
        "output_artifact",
        "downstream_consumer",
        "learning_sink",
        "last_match",
        "last_invocation",
        "last_success",
        "expiry",
        "kill_switch",
        "rollback",
    )
    for field in required_active:
        if not cap.get(field):
            raise AssertionError(f"active capability missing {field}")
    if not cap.get("outcome_links"):
        raise AssertionError("active capability missing outcome links")
    probes = cap.get("activation_evidence") or {}
    for probe in ACTIVE_PROBES:
        evidence = probes.get(probe) or {}
        if not evidence.get("passed"):
            raise AssertionError(f"active capability missing passing {probe}")
        if not evidence.get("checked_at"):
            raise AssertionError(f"active capability missing {probe} checked_at")
        if not evidence.get("ref"):
            raise AssertionError(f"active capability missing {probe} evidence ref")
    current = _now() if now is None else now
    if int(cap["expiry"]) <= current:
        raise AssertionError("active capability is expired")


def validate_ledger(path: Path = REG, *, create: bool = True) -> dict[str, Any]:
    capabilities = load(path, create=create)
    errors: list[dict[str, str]] = []
    for name, cap in sorted(capabilities.items()):
        try:
            validate_capability(cap)
        except (AssertionError, ValueError, TypeError) as exc:
            errors.append({"capability_id": name, "error": str(exc)})
    return {"path": str(path), "count": len(capabilities), "errors": errors, "valid": not errors}


def register(capability_id: str, record: dict[str, Any], path: Path = REG) -> None:
    with _locked(path):
        capabilities = _read_ledger_unlocked(path)["capabilities"] if path.exists() else {}
        if capability_id in capabilities:
            raise ValueError(f"capability already registered: {capability_id}")
        cap = {**_blank_capability(capability_id), **record, "capability_id": capability_id}
        validate_capability(cap)
        capabilities[capability_id] = cap
        _write_ledger_unlocked(path, capabilities)


def transition(
    capability_id: str,
    new_state: str,
    *,
    reason: str,
    evidence_refs: list[str] | None = None,
    path: Path = REG,
    timestamp: int | None = None,
) -> None:
    ts = _now() if timestamp is None else timestamp
    with _locked(path):
        capabilities = _read_ledger_unlocked(path)["capabilities"]
        if capability_id not in capabilities:
            raise ValueError(f"unknown capability: {capability_id}")
        cap = capabilities[capability_id]
        old_state = cap["status"]
        if new_state not in TRANSITIONS.get(old_state, set()):
            raise ValueError(f"illegal capability transition: {old_state} -> {new_state}")
        cap["status"] = new_state
        cap.setdefault("event_history", []).append(
            {
                "timestamp": ts,
                "type": "transition",
                "from": old_state,
                "to": new_state,
                "reason": reason,
                "evidence_refs": list(evidence_refs or []),
            }
        )
        validate_capability(cap, now=ts)
        _write_ledger_unlocked(path, capabilities)


def heartbeat(
    capability_id: str,
    event_type: str,
    *,
    ref: str | None = None,
    metadata: dict[str, Any] | None = None,
    timestamp: int | None = None,
    path: Path = REG,
    idempotency_key: str | None = None,
) -> bool:
    if event_type not in EVENT_FIELDS:
        raise ValueError(f"invalid capability event type: {event_type}")
    if (
        event_type
        in {"output", "consumer", "failure", "outcome", "outcome_amendment", "offer_amendment"}
        and not ref
    ):
        raise ValueError(f"{event_type} heartbeat requires ref")
    ts = _now() if timestamp is None else timestamp
    with _locked(path):
        capabilities = _read_ledger_unlocked(path)["capabilities"]
        if capability_id not in capabilities:
            raise ValueError(f"unknown capability: {capability_id}")
        cap = capabilities[capability_id]
        if idempotency_key and any(
            event.get("idempotency_key") == idempotency_key
            for event in cap.get("event_history") or []
        ):
            return False
        field = EVENT_FIELDS[event_type]
        if field:
            cap[field] = max(int(cap.get(field) or 0), ts)
        if event_type == "outcome" and ref not in cap["outcome_links"]:
            cap["outcome_links"].append(ref)
        event = {"timestamp": ts, "type": event_type}
        if ref:
            event["ref"] = ref
        if metadata:
            event["metadata"] = metadata
        if idempotency_key:
            event["idempotency_key"] = idempotency_key
        cap.setdefault("event_history", []).append(event)
        _write_ledger_unlocked(path, capabilities)
        return True


def production_heartbeat(
    capability_id: str,
    event_type: str,
    *,
    ref: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> bool:
    """Emit strict lifecycle evidence only inside the active orchestrator process."""
    if os.environ.get("ORCH_CAPABILITY_HEARTBEATS") != "1":
        return False
    heartbeat(capability_id, event_type, ref=ref, metadata=metadata)
    return True


def daily_heartbeat(
    capability_id: str,
    event_type: str,
    *,
    ref: str | None = None,
    metadata: dict[str, Any] | None = None,
    path: Path = REG,
) -> bool:
    """A heartbeat for HOT code paths, coalesced to at most one event per capability/type/day.

    `event_history` is append-only and uncapped, and `heartbeat` linear-scans it for the
    idempotency key — so a per-invocation heartbeat on a hot path degrades itself. Measured
    2026-08-09: `record_run` fires ~48×/day and completion events ~120×/day, which would add
    ~17,500 and ~44,000 events per YEAR to a single capability record, each one making the next
    scan and full-ledger rewrite slower.

    Daily coalescing bounds that to ~365 events/year while preserving the signal that actually
    matters for an always-on capability: it ran today. Per-invocation frequency is deliberately
    NOT retained — for something exercised by every recorded run, a count adds nothing that
    `last_invocation` does not already say, and the cost is unbounded growth.

    Returns True only when this is the first such event today. Honours the same production gate as
    `production_heartbeat`, so it is inert outside an active tick.
    """
    if os.environ.get("ORCH_CAPABILITY_HEARTBEATS") != "1":
        return False
    day = time.strftime("%Y-%m-%d", time.gmtime(_now()))
    return heartbeat(
        capability_id,
        event_type,
        ref=ref,
        metadata=metadata,
        path=path,
        idempotency_key=f"daily:{capability_id}:{event_type}:{day}",
    )


def record_probe(
    capability_id: str,
    probe: str,
    *,
    passed: bool,
    ref: str,
    detail: str | None = None,
    timestamp: int | None = None,
    path: Path = REG,
) -> None:
    """Attach auditable activation-probe evidence to a registered capability."""
    if probe not in ACTIVE_PROBES:
        raise ValueError(f"unknown activation probe: {probe}")
    if not ref:
        raise ValueError("activation probe requires an evidence ref")
    ts = _now() if timestamp is None else timestamp
    with _locked(path):
        capabilities = _read_ledger_unlocked(path)["capabilities"]
        if capability_id not in capabilities:
            raise ValueError(f"unknown capability: {capability_id}")
        cap = capabilities[capability_id]
        evidence = {"passed": bool(passed), "checked_at": ts, "ref": ref}
        if detail:
            evidence["detail"] = detail
        cap.setdefault("activation_evidence", {})[probe] = evidence
        cap.setdefault("event_history", []).append(
            {
                "timestamp": ts,
                "type": "activation_probe",
                "probe": probe,
                "passed": bool(passed),
                "ref": ref,
            }
        )
        validate_capability(cap, now=ts)
        _write_ledger_unlocked(path, capabilities)


def sweep(path: Path = REG, *, now: int | None = None) -> list[str]:
    ts = _now() if now is None else now
    with _locked(path):
        if not path.exists():
            return []
        capabilities = _read_ledger_unlocked(path)["capabilities"]
        retired = _expire_in_place(capabilities, ts)
        if retired:
            _write_ledger_unlocked(path, capabilities)
        return retired


def reconcile_all(path: Path = REG, *, now: int | None = None) -> dict[str, Any]:
    """Continuously refresh every immutable compiled version from Brain truth."""
    records = load(path, create=False)
    results, errors = [], []
    for capability_id, cap in sorted(records.items()):
        if not cap.get("capability_version_id"):
            continue
        try:
            results.append(reconcile_causal_lifecycle(capability_id, path=path, timestamp=now))
        except (AssertionError, OSError, ValueError) as exc:
            errors.append({"capability_id": capability_id, "error": str(exc)})
    return {"reconciled": results, "errors": errors, "valid": not errors}


def complete_verified_rollback(
    capability_id: str,
    *,
    rollback_proof: str,
    predecessor: str,
    path: Path = REG,
    timestamp: int | None = None,
) -> dict[str, Any]:
    """Retire only after an external target adapter proves rollback completion."""
    if not rollback_proof.startswith("sha256:"):
        raise ValueError("verified rollback requires a sha256 proof")
    now = _now() if timestamp is None else int(timestamp)
    with _locked(path):
        records = _read_ledger_unlocked(path)["capabilities"]
        cap = records.get(capability_id)
        if cap is None:
            raise ValueError(f"unknown capability: {capability_id}")
        pending = cap.get("rollback_pending") or {}
        if not pending:
            raise ValueError("capability has no pending rollback")
        if cap.get("predecessor") != predecessor or pending.get("predecessor") != predecessor:
            raise AssertionError("regressing canary rollback predecessor drifted")
        predecessor_cap = records.get(predecessor)
        if not predecessor_cap or predecessor_cap.get("status") != "active":
            raise AssertionError("rollback predecessor is not routable")
        if cap.get("status") != "retired":
            _transition_in_place(
                cap,
                "retired",
                reason=str(pending.get("reason") or "verified target rollback"),
                timestamp=now,
                evidence_ref=rollback_proof,
            )
        cap["rollback_result"] = {
            **pending,
            "phase": "verified",
            "rollback_proof": rollback_proof,
            "verified_at": now,
            "predecessor": predecessor,
        }
        cap["rollback_pending"] = None
        cap["next_transition"] = None
        validate_capability(cap, now=now)
        _write_ledger_unlocked(path, records)
        return json.loads(json.dumps(cap))


def attach_owner_question(
    capability_id: str,
    question: dict[str, Any],
    *,
    path: Path = REG,
    timestamp: int | None = None,
) -> dict[str, Any]:
    """Persist the bounded Brain question on the lifecycle record."""
    if not question.get("question_id") or question.get("status") not in {
        "open",
        "answered",
        "expired_default",
    }:
        raise ValueError("invalid owner-question reference")
    now = _now() if timestamp is None else int(timestamp)
    with _locked(path):
        records = _read_ledger_unlocked(path)["capabilities"]
        cap = records.get(capability_id)
        if cap is None:
            raise ValueError(f"unknown capability: {capability_id}")
        cap["owner_question"] = {**question, "recorded_at": now}
        cap["gate_reason"] = "bounded owner question; default keeps target unexported"
        cap["gate_evidence"] = question["question_id"]
        cap["evidence_threshold"] = "owner answer or expiring default"
        cap.setdefault("event_history", []).append(
            {
                "timestamp": now,
                "type": "owner_question",
                "question_id": question["question_id"],
                "status": question["status"],
            }
        )
        validate_capability(cap, now=now)
        _write_ledger_unlocked(path, records)
        return json.loads(json.dumps(cap))


def link_successor(
    retired_capability_id: str,
    successor_capability_id: str,
    *,
    path: Path = REG,
    timestamp: int | None = None,
) -> dict[str, Any]:
    """Link a new identity without rewriting the retired version or its outcomes."""
    now = _now() if timestamp is None else int(timestamp)
    with _locked(path):
        records = _read_ledger_unlocked(path)["capabilities"]
        retired = records.get(retired_capability_id)
        successor = records.get(successor_capability_id)
        if not retired or retired.get("status") != "retired":
            raise ValueError("successor link requires a retired predecessor version")
        if not successor or successor.get("status") in {"retired", "superseded"}:
            raise ValueError("successor link requires a live successor version")
        if retired.get("successor") not in {None, successor_capability_id}:
            raise ValueError("retired capability already has a different successor")
        retired["successor"] = successor_capability_id
        successor["supersedes"] = retired_capability_id
        retired.setdefault("event_history", []).append(
            {
                "timestamp": now,
                "type": "successor_linked",
                "successor": successor_capability_id,
                "preserved_outcome_links": list(retired.get("outcome_links") or []),
            }
        )
        _write_ledger_unlocked(path, records)
        return {
            "retired_capability_id": retired_capability_id,
            "successor_capability_id": successor_capability_id,
            "preserved_outcome_links": list(retired.get("outcome_links") or []),
        }


def summary(path: Path = REG, *, create: bool = True) -> dict[str, Any]:
    # A `create=False` read must STILL answer with the reconciled view. Every field this reports is
    # DECLARATION-owned — `status` (counted below), plus `gate_reason` and `matcher`, which
    # `classify_liveness` reads in the consumers — and `_reconcile_known_declarations` rewrites
    # those from `KNOWN_GATES` only inside a WRITING `load(create=True)`, i.e. in a different call
    # and possibly a different process. A raw read therefore lands on whichever side of that write
    # it happens to race, and nothing in its result says which side it got.
    #
    # That is the 2026-08-21 flake's root cause, relocated from a test into a REPORT:
    # `redirect-apply-bootstrap` sat at `shadow` on disk while the code declared `canary` until
    # reconciliation landed at 08:04:50, so a report generated in that window would have counted it
    # under a state the system was not acting on. The declared view is the right one to show,
    # because it is the state the system acts on.
    #
    # `load_declared` is what makes that safe: it reconciles the IN-MEMORY copy and writes NOTHING.
    # All three `create=False` callers — `periodic_report.build_report`, `features.summary`, and
    # `dry_seam_audit.audit_dry_seams` (whose `invoked_without_outcomes` classification is a gate) —
    # read the SHARED live ledger at ~/.codex/orchestrator/capabilities.json, and a report must not
    # mutate shared state to render itself. Passing `create=create` here would do exactly that.
    capabilities = load(path, create=True) if create else load_declared(path)
    counts = {state: 0 for state in CANONICAL_STATES}
    for cap in capabilities.values():
        counts[cap["status"]] = counts.get(cap["status"], 0) + 1
    active_without_edges = []
    for name, cap in capabilities.items():
        if cap["status"] == "active":
            try:
                validate_capability(cap)
            except AssertionError as exc:
                active_without_edges.append({"capability_id": name, "error": str(exc)})
    return {
        "path": str(path),
        "total": len(capabilities),
        "counts_by_status": counts,
        "active_without_edges": active_without_edges,
        "capabilities": capabilities,
    }


# Matcher kinds whose capabilities produce a REPORT or a RECORD, never a delivery. Derived rather
# than stored: `matcher.kind` already carries this, and adding a parallel field would be a second
# inventory of the same fact. Anything not listed here influences a run that can terminate, so it
# is expected to carry an outcome edge and a missing one is a real linkage gap.
OBSERVER_MATCHER_KINDS = frozenset(
    {
        "tick_phase",  # cadence steps that emit a report
        "feedback_event",  # the Brain recording its own events
        "test_gate",  # suite checks
        "tick_preflight",  # pre-tick guards
    }
)


def is_observer(cap: dict[str, Any]) -> bool:
    """Does this capability produce a report/record rather than a delivery?"""
    return str((cap.get("matcher") or {}).get("kind") or "") in OBSERVER_MATCHER_KINDS


def classify_liveness(
    cap: dict[str, Any],
    *,
    now: int | None = None,
    stale_days: int = 14,
    matching_work: bool = False,
) -> str:
    """Classify one capability from its own lifecycle evidence."""
    status = cap.get("status")
    if status in {"retired", "superseded"}:
        return status
    last_match = cap.get("last_match")
    last_invocation = cap.get("last_invocation")
    last_success = cap.get("last_success")
    last_outcome = max(
        (
            int(event.get("timestamp") or 0)
            for event in cap.get("event_history") or []
            if event.get("type") == "outcome"
        ),
        default=0,
    )
    # OBSERVERS CANNOT HAVE DELIVERY OUTCOMES, so calling them a measurement gap is a category
    # error — the same shape as asking a `{"kind": "transport"}` capability a task_type question.
    # A cadence report or a feedback-event recorder never merges a PR and never earns a durability
    # verdict, so `invoked_without_outcomes` was a state they could never leave: 8 of the 16
    # capabilities in that bucket were permanently mislabeled, and the advice attached to it ("fix
    # outcome linkage") described work that does not exist for them. Their liveness question is
    # whether they RAN, which `last_invocation` already answers.
    if is_observer(cap) and last_invocation:
        return "observing"
    # "invoked_without_outcomes" must mean NO outcome evidence — not "the newest invocation is
    # newer than the newest outcome". Outcomes legitimately lag invocations by days (a run has to
    # merge and survive a durability window), so the strict-timestamp form was structurally
    # guaranteed for any frequently-invoked capability: role-triage runs ~98x/week, so a fresh
    # invocation always outran its newest outcome and it stayed flagged as a measurement gap even
    # holding 12 linked terminal outcomes. Same failure family as the other latched metrics — a
    # label whose clear path is blocked by the very activity it is meant to reward.
    has_outcome_evidence = bool(last_outcome) or bool(cap.get("outcome_links"))
    # A GATE THAT BLOCKS THE DELIVERING CODE PATH IS NOT A MEASUREMENT GAP. Some capabilities hold
    # historical invocations from a window when their switch was on, and cannot produce an outcome
    # today because the switch is off — `thompson-hybrid-routing` (never chooses while
    # ORCH_EXPLORATION_MODE is epsilon-greedy) and `range-lane-rollout` (its heartbeats fire only on
    # the live-apply branch; PREVIEW never reaches it). Labelling those `invoked_without_outcomes`
    # and advising "fix outcome linkage" describes work that cannot be done until the switch moves,
    # which is the same unescapable-label shape as the latched metric and the observer category
    # error above.
    #
    # DECLARED, NOT INFERRED, and deliberately opt-in: 16 of 39 ledger capabilities carry a
    # `gate_reason`, and blanket-reordering the two checks would silently reclassify all of them —
    # including `issue-readiness`, whose gate covers only its LABEL WRITES while the assessment runs
    # every day and really does influence what the opener picks. So a capability must say so, and
    # absent the flag the behaviour is unchanged.
    if (
        cap.get("gate_blocks_execution")
        and cap.get("gate_reason")
        and status in {"generated", "validated", "wired", "shadow", "exercised", "canary"}
    ):
        return "deliberately_gated"
    # MATCHED-BUT-NOT-INVOKED IS A DISPATCH GAP, WHICH MEANS IT ONLY APPLIES TO SOMETHING THAT WAS
    # SUPPOSED TO BE DISPATCHED. This check used to run FIRST, above both `observing` and
    # `deliberately_gated`, and that made it the fourth instance of the unescapable label the three
    # comments above exist to fix — with the same signature: a class of capability that can never
    # leave the bucket, and advice ("find out why it did not run") whose answer is already recorded
    # on the row.
    #
    # Two whole classes were captured, and both are structural rather than unlucky:
    #
    #   OBSERVERS are matched by a tick phase, so `last_match` advances every cadence tick while
    #   `last_invocation` advances only when the phase actually fires. last_match > last_invocation
    #   is therefore the NORMAL resting state of a healthy observer, not a symptom.
    #   `live-keepalive-supervisor` sat here with matcher tick_phase/keepalive-stage2-plan.
    #
    #   DECLARED-GATED capabilities cannot be invoked at all while the switch is off, so every match
    #   after the gate closed widens the gap permanently. `range-lane-rollout` sat here with
    #   `gate_blocks_execution` set and its reason recorded, which is the row saying in advance
    #   exactly why it did not run.
    #
    # Both were red on main for anyone with a populated ledger and INVISIBLE to CI, which bootstraps
    # an empty one: with no rows, the two tests skipped with a named reason and the suite went green.
    # Moving the check below `observing` and the DECLARED gate leaves its real meaning intact — a
    # capability that should have been dispatched and was not — and does not touch the weaker
    # gate_reason-only branch further down, so nothing is reclassified by inference.
    if (last_match or matching_work) and (
        not last_invocation or (last_match and int(last_match) > int(last_invocation))
    ):
        return "matched_not_invoked"
    if last_invocation and not has_outcome_evidence:
        return "invoked_without_outcomes"
    current = _now() if now is None else now
    if status == "active" and (
        not last_success or int(last_success) < current - stale_days * 86400
    ):
        return "stale_active"
    if cap.get("gate_reason") and status in {
        "generated",
        "validated",
        "wired",
        "shadow",
        "exercised",
        "canary",
    }:
        return "deliberately_gated"
    if status in {"generated", "validated", "wired", "shadow"} and cap.get("outcome_links"):
        return "wired_but_dry"
    if not last_match and not matching_work:
        return "no_matching_work"
    return "healthy"


# ---------------------------------------------------------------------------
# Layer 1 (2026-08-09): make NON-USE legible.
#
# The inventory has always answered "what state is this capability in". It could not answer the
# question that actually matters here: WHY is it not being used, and what would change that.
# Measured at design time: 27 of 33 capabilities had never been invoked and 31 had zero outcomes,
# while `_causal_readiness` requires >=3 independent durable reuses to lift a gate. So the gates
# were not awaiting a decision — they were starved of the evidence that would lift them, and no
# amount of human review could have moved one. These three functions turn that into numbers:
# how often a capability is actually used, how far its gate is from liftable, and the single next
# action that would move it. Everything is derived from the EXISTING record (append-only
# event_history + persisted causal_evidence.readiness) — no second inventory, no Brain query.
# ---------------------------------------------------------------------------

RETIRE_CANDIDATE_DAYS = 90  # never matched this long => retire, don't manufacture work for it


def _capability_age_ts(cap: dict[str, Any]) -> int:
    """Earliest event timestamp — how long this capability has had a chance to be used.

    CAVEAT: this is time-in-LEDGER, not time-since-built. Capabilities migrated into the ledger in
    one batch all share that migration timestamp, so a long-dormant feature can look young. It
    therefore under-reports retirement candidates (a safe direction — it never proposes retiring
    something prematurely), and the bound becomes accurate as records age past the migration.
    """
    stamps = [int(e.get("timestamp") or 0) for e in (cap.get("event_history") or [])]
    return min((s for s in stamps if s), default=0)


def usage_rate(
    cap: dict[str, Any], *, now: int | None = None, window_days: int = 28
) -> dict[str, Any]:
    """How often this capability is ACTUALLY used, not just when it was last seen.

    A last-seen timestamp cannot distinguish "runs constantly" from "ran once in March", which is
    exactly the discrimination needed to spot a capability decaying toward dormancy. Counted from
    the append-only event_history over a trailing window.
    """
    current = _now() if now is None else now
    cutoff = current - window_days * 86400
    events = cap.get("event_history") or []
    counts = {"match": 0, "invocation": 0, "outcome": 0}
    for event in events:
        etype = str(event.get("type") or "")
        if etype in counts and int(event.get("timestamp") or 0) >= cutoff:
            counts[etype] += 1
    weeks = max(window_days / 7.0, 1e-9)
    age_ts = _capability_age_ts(cap)
    return {
        "window_days": window_days,
        "matches": counts["match"],
        "invocations": counts["invocation"],
        "outcomes": counts["outcome"],
        "invocations_per_week": round(counts["invocation"] / weeks, 3),
        "outcomes_per_week": round(counts["outcome"] / weeks, 3),
        # Distinguishes "new, not yet exercised" from "old and never exercised" — only the second
        # is a retirement candidate.
        "age_days": int((current - age_ts) / 86400) if age_ts else None,
        "ever_invoked": bool(cap.get("last_invocation")),
    }


def evidence_debt(cap: dict[str, Any]) -> dict[str, Any]:
    """How much more durable evidence before this capability's gate COULD lift.

    Turns "deliberately_gated" into a number: `remaining` is how many further independent durable
    reuses the promotion policy still wants. `blocked_by` names any criterion that a further
    success cannot fix on its own (a recorded failure or rework already breaches a zero-tolerance
    bound, so more volume will not help — the defect must be addressed first).
    """
    policy = {**DEFAULT_PROMOTION_POLICY, **(cap.get("lifecycle_policy") or {})}
    readiness = (cap.get("causal_evidence") or {}).get("readiness") or {}
    have = len(readiness.get("durable_subjects") or [])
    need = int(policy["min_independent_durable_reuse"])
    failures = int(readiness.get("failures") or 0)
    rework = int(readiness.get("rework") or 0)
    blocked_by = []
    if failures > int(policy["max_failures"]):
        blocked_by.append(f"{failures} failure(s) over the max of {policy['max_failures']}")
    if rework > int(policy["max_rework"]):
        blocked_by.append(f"{rework} rework over the max of {policy['max_rework']}")
    return {
        "durable_reuses": have,
        "required": need,
        "remaining": max(0, need - have),
        "ready": bool(readiness.get("ready")),
        "blocked_by": blocked_by,  # non-empty => more volume alone will NOT lift the gate
        "has_evidence": bool(readiness),
    }


# ---------------------------------------------------------------------------
# Layer 2 (2026-08-09): evaluate a GATE's own threshold, for any status.
#
# `reconcile_causal_lifecycle` consults `readiness` only for `canary`; `shadow`/`wired` advance on a
# single terminal outcome and NOTHING consults the gate's own `evidence_threshold`. Those thresholds
# are already written down — several in directly checkable prose ("five linked outcomes, three
# durable, and at least one disagreement") — but as text no code reads.
#
# This makes them evaluable, with one hard rule: a gate is NEVER reported ready while any criterion
# is unevaluated. Prose that was never encoded, and named observations the causal record cannot
# supply (a rejected counterfactual, an external review recommendation), both count as unevaluated.
# Silence must not read as a pass — that is the same failure that let a dead model pin report `ok`.
#
# Layer 2 REPORTS readiness; it does not lift gates. Auto-flipping a deliberate safety switch is a
# separate, larger decision (CLAUDE.md §4) and stays a deliberate act.
# ---------------------------------------------------------------------------

GATE_BOUND_KEYS = frozenset(
    {
        "min_linked_outcomes",  # terminal outcomes joined to this capability
        "min_independent_durable_reuse",  # distinct durably-successful subjects
        "max_failures",
        "max_rework",
        "max_evidence_age_days",
    }
)


def gate_policy(cap: dict[str, Any]) -> dict[str, Any]:
    """This capability's gate threshold in machine-checkable form.

    Precedence: explicit `gate_criteria` > `lifecycle_policy` > `DEFAULT_PROMOTION_POLICY`.
    `gate_criteria["requires"]` names observations the causal record cannot supply; any of them
    makes the gate un-auto-liftable by design rather than by omission.
    """
    policy = {**DEFAULT_PROMOTION_POLICY, **(cap.get("lifecycle_policy") or {})}
    criteria = dict(cap.get("gate_criteria") or {})
    requires = [str(r) for r in (criteria.pop("requires", None) or [])]
    bounds = {k: v for k, v in criteria.items() if k in GATE_BOUND_KEYS}
    unknown = sorted(set(criteria) - GATE_BOUND_KEYS)
    policy.update(bounds)
    return {
        "policy": policy,
        "requires": requires,
        "unknown_criteria": unknown,  # named but not understood => treated as unevaluated
        "encoded": bool(bounds or requires),  # False => the threshold is still only prose
    }


def gate_readiness(cap: dict[str, Any], *, now: int | None = None) -> dict[str, Any]:
    """Whether this capability's gate COULD lift, with every unevaluated criterion named.

    `ready` is True only when the gate is encoded, evidence exists, every bound is satisfied, and
    nothing is unevaluated. Everything else reports False WITH the reason, so a gate can never pass
    by silence.
    """
    current = _now() if now is None else now
    if not cap.get("gate_reason"):
        return {
            "gated": False,
            "ready": False,
            "criteria": {},
            "unevaluated": [],
            "reason": "not gated",
        }
    spec = gate_policy(cap)
    policy = spec["policy"]
    readiness = (cap.get("causal_evidence") or {}).get("readiness") or {}
    unevaluated: list[str] = []
    if not spec["encoded"]:
        unevaluated.append(
            f"threshold is prose only, not encoded: {cap.get('evidence_threshold') or 'unstated'}"
        )
    unevaluated += [
        f"requires an observation the causal record cannot supply: {name}"
        for name in spec["requires"]
    ]
    unevaluated += [f"unrecognised gate criterion: {name}" for name in spec["unknown_criteria"]]
    if not readiness:
        unevaluated.append("no causal evidence recorded yet")

    durable = len(readiness.get("durable_subjects") or [])
    linked = int(readiness.get("terminal_outcomes") or 0)
    failures = int(readiness.get("failures") or 0)
    rework = int(readiness.get("rework") or 0)
    latest = int(readiness.get("latest_evidence_ts") or 0)
    criteria = {
        "independent_durable_reuse": durable >= int(policy["min_independent_durable_reuse"]),
        "failures": failures <= int(policy["max_failures"]),
        "rework": rework <= int(policy["max_rework"]),
    }
    if "min_linked_outcomes" in policy:
        criteria["linked_outcomes"] = linked >= int(policy["min_linked_outcomes"])
    if readiness:
        criteria["evidence_age"] = bool(
            latest and current - latest <= int(policy["max_evidence_age_days"]) * 86400
        )

    unmet = sorted(k for k, ok in criteria.items() if not ok)
    ready = bool(criteria) and not unmet and not unevaluated
    if ready:
        reason = "all encoded criteria satisfied"
    elif unevaluated:
        reason = unevaluated[0]
    else:
        reason = "unmet: " + ", ".join(unmet)
    return {
        "gated": True,
        "ready": ready,
        "criteria": criteria,
        "unmet": unmet,
        "unevaluated": unevaluated,
        "encoded": spec["encoded"],
        "reason": reason,
        "observed": {
            "durable_reuses": durable,
            "linked_outcomes": linked,
            "failures": failures,
            "rework": rework,
        },
        "required": {k: policy[k] for k in sorted(GATE_BOUND_KEYS & set(policy))},
    }


def _has_default_off_switch(cap: dict[str, Any]) -> bool:
    """True when this capability is held closed by a documented default-off flag.

    Read from the capability's own declared `kill_switch`/`rollback` prose rather than a second list
    of flag names, so a capability cannot drift out of this check by being renamed. Deliberately
    conservative: it looks for the documented phrasing this ledger already uses, and an unmatched
    switch reads as NOT default-off, which keeps a genuinely feedable capability feedable.
    """
    text = " ".join(
        str(cap.get(field) or "") for field in ("kill_switch", "rollback", "gate_note")
    ).lower()
    return "default-off" in text or "default off" in text


def unblock(
    cap: dict[str, Any], *, liveness: str | None = None, now: int | None = None
) -> dict[str, Any]:
    """The single next action that would move this capability, and whether it is worth feeding.

    `feed` is the field Layer 3's acquisition lane must respect: routing real capacity at a
    capability that should be RETIRED manufactures work for a feature nobody wants. Only
    capabilities that are plausibly useful but evidence-starved are worth feeding.
    """
    current = _now() if now is None else now
    live = liveness or classify_liveness(cap, now=current)
    rate = usage_rate(cap, now=current)
    debt = evidence_debt(cap)
    age = rate["age_days"]
    stale_enough = age is not None and age >= RETIRE_CANDIDATE_DAYS

    if live in {"retired", "superseded"}:
        return {
            "blocker": live,
            "action": "none",
            "feed": False,
            "needs_trigger": False,
            "retire_candidate": False,
        }
    if live == "no_matching_work":
        # UNREALIZED, not dead. A capability that never matched has no TRIGGER wired — that is a
        # gap in how we invoke it, not evidence it is worthless. Owner direction 2026-08-09: the
        # purpose of this system is to MAXIMIZE capability use, so "never matched" is an
        # enablement queue. Retirement is a last resort after a trigger has been tried, never the
        # default reading of silence.
        # Two distinct states share the `no_matching_work` liveness class, and telling a capability
        # that has a trigger it "needs a trigger" is simply wrong. classify_liveness keys on whether
        # work ever MATCHED at runtime; whether a trigger is DECLARED is a different question.
        if cap.get("matcher"):
            return {
                "blocker": "trigger declared, but no work has matched it yet",
                "action": (
                    "wait for matching work, or widen the matcher if this work occurs "
                    "under a different task type"
                ),
                "feed": False,
                "needs_trigger": False,  # already wired — not the enablement queue
                "retire_candidate": False,
            }
        return {
            "blocker": "no trigger wired — nothing routes work here yet",
            "action": (
                "define a matcher/trigger so this capability can be reached"
                + (" (long-unused: confirm it is still wanted)" if stale_enough else "")
            ),
            "feed": False,  # feeding cannot help until something routes here
            "needs_trigger": True,  # the enablement queue
            "retire_candidate": False,
        }
    if live == "observing":
        return {
            "blocker": "none — an observer has no delivery outcome to link",
            "action": "none: confirm it still runs on cadence. Do NOT chase outcome linkage or "
            "durable reuse here; a report cannot merge a PR, and a gate that demands it "
            "would be permanently unsatisfiable",
            "feed": False,
            "needs_trigger": False,
            "retire_candidate": False,
        }
    if live == "invoked_without_outcomes":
        # THREE DIFFERENT SITUATIONS SHARE THIS CLASS, and for two of them "fix outcome linkage"
        # describes work that DOES NOT EXIST. That is the third time this exact shape has appeared:
        # the observer category error told reports to earn delivery outcomes they cannot produce,
        # and the gated case told switched-off capabilities to fix linkage a switch was blocking.
        # Both were fixed by making the CLASSIFICATION honest. Here the classification is literally
        # true -- there really is no outcome evidence -- so the fix is one layer down, in the ADVICE.
        #
        # Why it matters more than wording: `invoked_without_outcomes` is a `dry_seam_audit` FAIL,
        # so a permanently-wrong FAIL with unactionable advice is what teaches a reader to ignore
        # the gate -- the same harm the tick_env fix named ("re-run until green" is how a real red
        # gets missed). Each branch below is decided from evidence the ledger ALREADY holds; nothing
        # new is stored and no capability self-declares its way out.

        # (a) DEFERRED BY DECISION. A recorded deferral with a machine-checkable revisit condition
        # is not debt -- telling it to "fix linkage" contradicts a decision someone already made.
        notes = str(cap.get("notes") or "")
        if re.search(
            r"\bREVISIT TRIGGER\b|deliberately NOT built|deferred with a", notes, re.IGNORECASE
        ):
            return {
                "blocker": "no outcome linked -- deferred by an explicit decision",
                "action": (
                    "none: a resolver for this was deliberately not built and the deferral "
                    "records its own revisit condition. Re-read the ledger `notes` before "
                    "treating this as debt"
                ),
                "feed": False,
                "needs_trigger": False,
                "retire_candidate": False,
            }

        # (b) USAGE DROUGHT, not a linkage bug. An encoded gate with real bounds, zero accepted uses
        # ever, and a stale last_invocation means the outcome path is intact and simply unexercised
        # -- `role-prompt` ran ONCE, its proposal was REJECTED, so it correctly inherits no outcome.
        # Manufacturing one for an advisory run is exactly what the un-gameable label forbids.
        last_inv = cap.get("last_invocation")
        drought_age = (current - int(last_inv)) / 86400.0 if last_inv else None
        if (
            cap.get("gate_criteria")
            and not cap.get("outcome_links")
            and drought_age is not None
            and drought_age >= 14
        ):
            return {
                "blocker": f"no ACCEPTED use yet -- last invocation {int(drought_age)}d ago",
                "action": (
                    "wait for an accepted use; this is a USAGE drought, not a measurement "
                    "gap. The outcome path is declared and its gate_criteria are encoded, so "
                    "there is no linkage to fix -- do NOT hand-link an outcome"
                ),
                "feed": False,
                "needs_trigger": False,
                "retire_candidate": False,
            }

        # (c) A GENUINE MEASUREMENT GAP -- the original case, unchanged.
        return {
            "blocker": "runs, but no outcome is linked",
            "action": "fix outcome linkage — this is a MEASUREMENT gap, not a usage gap. This "
            "capability influences runs that terminate, so a heartbeat carrying the "
            "run_id it influenced is what lets capability_outcome_bridge write the edge",
            "feed": False,  # more runs produce more unmeasured runs
            "needs_trigger": False,
            "retire_candidate": False,
        }
    if live == "matched_not_invoked":
        return {
            "blocker": "matched but a gate blocked invocation",
            "action": "lift the gate, or accept it is deliberately off",
            "feed": False,
            "needs_trigger": False,
            "retire_candidate": False,
        }
    if live == "wired_but_dry":
        # Two very different situations share this class. One is genuinely dry. The other is a
        # capability running at volume WITH outcomes that simply never got promoted out of its
        # initial state — abcd-experiment sits at 21 invocations/week with 143 outcomes. Telling
        # someone to hunt a dead producer there sends them looking for a problem that isn't there.
        if rate["invocations"] or debt["durable_reuses"] or (cap.get("outcome_links") or []):
            return {
                "blocker": "running with evidence, but never promoted out of its initial state",
                "action": "promote it: the evidence exists, the lifecycle record is just stale",
                "feed": False,
                "needs_trigger": False,
                "retire_candidate": False,
            }
        return {
            "blocker": "wired but nothing flows through it",
            "action": "find the dead upstream producer",
            "feed": False,
            "retire_candidate": False,
        }
    if live == "stale_active":
        return {
            "blocker": "marked active but no recent success",
            "action": "verify it still works, or transition it back",
            "feed": False,
            "needs_trigger": False,
            "retire_candidate": False,
        }
    if live == "deliberately_gated":
        gate = gate_readiness(cap, now=current)
        if debt["blocked_by"]:
            return {
                "blocker": "gate cannot lift on volume alone: " + "; ".join(debt["blocked_by"]),
                "action": "address the recorded failure/rework first",
                "feed": False,
                "needs_trigger": False,
                "retire_candidate": False,
            }
        if gate["ready"]:
            return {
                "blocker": "none — every encoded criterion satisfied",
                "action": "READY TO LIFT: promote this gate",
                "feed": False,
                "needs_trigger": False,
                "retire_candidate": False,
            }
        if not gate["encoded"]:
            # The threshold exists only as prose, so nothing can ever evaluate it. Feeding this
            # capability would accumulate evidence against a bar no code can check.
            return {
                "blocker": "gate threshold is prose, not encoded",
                "action": (
                    "define a machine-checkable threshold "
                    "(capabilities.gate_criteria) before feeding this gate"
                ),
                "feed": False,
                "needs_trigger": False,
                "retire_candidate": False,
            }
        if gate["unevaluated"]:
            return {
                "blocker": gate["reason"],
                "action": "wire the missing observation, or accept the gate stays manual",
                "feed": False,
                "needs_trigger": False,
                "retire_candidate": False,
            }
        # A DEFAULT-OFF SWITCH CANNOT BE FED. Found 2026-08-22 while building Layer 3: the only two
        # capabilities this branch called feedable (range-lane-rollout, synthesis-promotion) are both
        # held by a documented default-off flag ("restore documented default-off gate"). Routing work
        # at a switched-off capability manufactures work it cannot execute, so no durable reuse is
        # produced, so the debt never falls, so it is fed again next cycle -- forever. The drain is
        # blocked by the very switch the feed ignored, which is this workspace's signature defect.
        #
        # Reported, never silently dropped: the remaining count still shows, so the capability stays
        # visible as evidence-starved. What changes is that capacity is not spent on it until the
        # owner flips the switch, which is a decision only the owner makes.
        if _has_default_off_switch(cap):
            return {
                "blocker": (
                    f"needs {debt['remaining']} more independent durable reuse(s), but a "
                    f"documented default-off switch prevents any from being produced"
                ),
                "action": "owner decision: flip the documented default-off gate, or leave it off",
                "feed": False,
                "needs_trigger": False,
                "retire_candidate": False,
            }
        return {
            "blocker": f"needs {debt['remaining']} more independent durable reuse(s)",
            "action": f"route {debt['remaining']} matching item(s) here to satisfy the gate",
            "feed": True,  # the one case worth spending capacity on
            "needs_trigger": False,
            "retire_candidate": False,
        }
    return {
        "blocker": "none",
        "action": "none",
        "feed": False,
        "needs_trigger": False,
        "retire_candidate": False,
    }


def usage_report(report: dict[str, Any], *, now: int | None = None) -> dict[str, Any]:
    """Per-capability usage + debt + next action, plus the roll-ups a digest needs."""
    current = _now() if now is None else now
    rows = []
    for name, cap in sorted(report["capabilities"].items()):
        live = classify_liveness(cap, now=current)
        rows.append(
            {
                "capability_id": name,
                "status": cap.get("status"),
                "liveness": live,
                "usage": usage_rate(cap, now=current),
                "debt": evidence_debt(cap),
                "gate": gate_readiness(cap, now=current),
                "unblock": unblock(cap, liveness=live, now=current),
            }
        )
    return {
        "generated_at": current,
        "total": len(rows),
        "ready_to_lift": [
            r["capability_id"] for r in rows if r["unblock"]["action"].startswith("READY TO LIFT")
        ],
        "promotable": [
            r["capability_id"]
            for r in rows
            if r["unblock"]["blocker"].startswith("running with evidence")
        ],
        "worth_feeding": [r["capability_id"] for r in rows if r["unblock"]["feed"]],
        # The ENABLEMENT queue: capabilities that exist and work but have no trigger routing work
        # to them. This is the list to shorten — the point of the system is to increase capability
        # use, so an unused capability is unfinished wiring, not a candidate for deletion.
        "needs_trigger": [r["capability_id"] for r in rows if r["unblock"]["needs_trigger"]],
        "retire_candidates": [r["capability_id"] for r in rows if r["unblock"]["retire_candidate"]],
        "measurement_gaps": [
            r["capability_id"] for r in rows if r["liveness"] == "invoked_without_outcomes"
        ],
        # Gates whose threshold no code can check. These are NOT ready and NOT feedable — they are
        # waiting on a definition, which is the one thing evidence cannot supply.
        "threshold_undefined": [
            r["capability_id"] for r in rows if r["gate"]["gated"] and not r["gate"]["encoded"]
        ],
        # Capabilities for which the causal reconciler has NEVER produced readiness. Distinct from
        # "short of evidence": the evidence pipeline itself never ran, so a debt of "3 more durable
        # reuses" describes an unread meter, not a measured shortfall. reconcile_causal_lifecycle
        # raises unless a capability carries immutable version lineage, and evidence only reaches it
        # through influence_edges rows tagged with capability_id — check BOTH before reading a zero
        # here as "this capability did no work" (2026-08-09).
        "readiness_never_computed": [
            r["capability_id"] for r in rows if not r["debt"]["has_evidence"]
        ],
        "gate_encoded": [
            r["capability_id"] for r in rows if r["gate"]["gated"] and r["gate"]["encoded"]
        ],
        "never_invoked": [r["capability_id"] for r in rows if not r["usage"]["ever_invoked"]],
        "active_last_28d": [r["capability_id"] for r in rows if r["usage"]["invocations"] > 0],
        "rows": rows,
    }


def format_usage_report(usage: dict[str, Any]) -> str:
    """Digest-shaped rendering: the roll-ups first, because those are the only actionable lines."""
    lines = [
        "# Orchestrator capability usage",
        "",
        f"{usage['total']} capabilities · {len(usage['active_last_28d'])} used in the last 28d · "
        f"{len(usage['never_invoked'])} never invoked",
        "",
    ]
    # READY TO LIFT has a DENOMINATOR. Reported bare it is a permanently-zero number that reads as
    # "nothing is progressing": of the gated capabilities, most state their threshold as prose no
    # code can check, and a few name observations the causal record cannot supply — so those can
    # never lift no matter how good the evidence is. Naming the reachable denominator turns a
    # 0 that looks like failure into a 0 that means "none of the 3 checkable gates is satisfied yet",
    # and makes the prose-gate backlog the actionable number it actually is.
    encoded = usage.get("gate_encoded") or []
    undefined = usage.get("threshold_undefined") or []
    gated_total = len(encoded) + len(undefined)
    if gated_total:
        if undefined:
            lines += [
                f"Gate promotion is reachable for {len(encoded)} of {gated_total} gated "
                f"capabilities; the other {len(undefined)} state their threshold in prose no code "
                f"can check, so no amount of evidence can lift them. Encode a `gate_criteria` "
                f"bound to make one checkable.",
                "",
            ]
        else:
            lines += [
                f"All {gated_total} gated capabilities have a machine-checkable threshold. A gate "
                f"below is held by either unmet bounds (evidence will satisfy it) or a named "
                f"`requires` observation the causal record cannot yet supply (something must be "
                f"built, or the gate is deliberately unliftable).",
                "",
            ]
    for label, key in (
        (
            f"READY TO LIFT (gate satisfied; {len(encoded)} gates are machine-checkable)",
            "ready_to_lift",
        ),
        ("PROMOTABLE (running with evidence; lifecycle record is stale)", "promotable"),
        ("MEASUREMENT GAPS (runs, but nothing is recorded)", "measurement_gaps"),
        ("WORTH FEEDING (starved, would lift with N more uses)", "worth_feeding"),
        ("THRESHOLD UNDEFINED (gate stated in prose no code can check)", "threshold_undefined"),
        (
            "READINESS NEVER COMPUTED (evidence pipeline has not run — an unread meter, "
            "not a measured shortfall)",
            "readiness_never_computed",
        ),
        ("NEEDS A TRIGGER (built and working; nothing routes work to it yet)", "needs_trigger"),
    ):
        items = usage.get(key) or []
        lines.append(f"## {label}: {len(items)}")
        lines.extend(f"- {name}" for name in items)
        lines.append("")
    lines += [
        "| Capability | State | Liveness | Inv/wk | Durable | Need | Next action |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    for row in usage["rows"]:
        d = row["debt"]
        lines.append(
            f"| {row['capability_id']} | {row['status']} | {row['liveness']} | "
            f"{row['usage']['invocations_per_week']} | {d['durable_reuses']} | {d['required']} | "
            f"{row['unblock']['action']} |"
        )
    return "\n".join(lines) + "\n"


def format_inventory(report: dict[str, Any]) -> str:
    lines = [
        "# Orchestrator capability inventory",
        "",
        f"Source: `{report['path']}`",
        "",
        "| Capability | State | Liveness | Last match | Last invocation | Last success | Outcomes | Expiry | Gate / next transition |",
        "|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for name, cap in sorted(report["capabilities"].items()):
        gate = cap.get("gate_reason") or ""
        if cap.get("next_transition"):
            gate = f"{gate}; next={cap['next_transition']}".strip("; ")
        lines.append(
            f"| {name} | {cap['status']} | {classify_liveness(cap)} | "
            f"{cap.get('last_match') or ''} | {cap.get('last_invocation') or ''} | "
            f"{cap.get('last_success') or ''} | {len(cap.get('outcome_links') or [])} | "
            f"{cap.get('expiry') or ''} | {gate} |"
        )
    return "\n".join(lines) + "\n"


def _selftest() -> None:
    with tempfile.TemporaryDirectory(prefix="capabilities-selftest-") as td:
        root = Path(td)
        features = root / "features.json"
        ledger = root / "capabilities.json"
        features.write_text(
            json.dumps(
                {
                    "old-hardened": {
                        "problem": "legacy feature",
                        "maturity": "hardened",
                        "module": "old.py",
                    }
                }
            )
        )
        migrated = migrate_features_to_capabilities(features, ledger, now=100)
        assert migrated["capabilities"]["old-hardened"]["status"] == "generated"
        assert migrated["capabilities"]["old-hardened"]["last_invocation"] is None
        assert all(cap["status"] != "active" for cap in migrated["capabilities"].values())

        stale = _blank_capability("role-prompt")
        stale.update(
            {
                "status": "wired",
                "entrypoint": "roles.py:old_prompt",
                "flags_defaults": {"dispatch": False},
            }
        )
        assert _reconcile_known_declarations({"role-prompt": stale}, 101) is True
        assert stale["status"] == KNOWN_GATES["role-prompt"]["status"]
        assert stale["entrypoint"] == KNOWN_GATES["role-prompt"]["entrypoint"]
        assert stale["flags_defaults"]["ORCH_ROLE_SHADOW"] == "1"

        # `load_declared` must answer with the RECONCILED view and leave the FILE alone. Both
        # halves are load-bearing: without the reconcile, a reader asserting on a declaration-owned
        # field races whoever writes it next (that is the 2026-08-21 flake); without the
        # write-freedom, a test would mutate shared live state to ask a read-only question.
        declared_path = root / "declared.json"
        drifted = _blank_capability("role-prompt")
        drifted.update(
            {
                "status": "shadow",
                "entrypoint": "roles.py:ancient",
                "matcher": {"kind": "evidence_gate", "name": "stale"},
            }
        )
        _write_ledger_unlocked(declared_path, {"role-prompt": drifted})
        before = declared_path.read_bytes()
        reconciled = load_declared(declared_path)["role-prompt"]
        assert reconciled["matcher"] == KNOWN_GATES["role-prompt"]["matcher"], reconciled["matcher"]
        assert reconciled["entrypoint"] == KNOWN_GATES["role-prompt"]["entrypoint"], reconciled
        assert declared_path.read_bytes() == before, "load_declared must not write the ledger"
        # ...and the raw read must still show the drift, or the two functions have collapsed into
        # one and the caller can no longer tell disk state from declared state.
        raw = load(declared_path, create=False)["role-prompt"]
        assert raw["matcher"] == {"kind": "evidence_gate", "name": "stale"}, raw["matcher"]

        # `summary(create=False)` MUST REPORT THE DECLARED STATE, and must not write to get it.
        # Same root cause as the flake above, one layer out: `summary` counts `status`, which is
        # declaration-owned, so a report rendered between a code declaration changing and the next
        # reconciling `load()` would count a capability under a state the system is not acting on —
        # a report that lies about lifecycle state. The three `create=False` callers
        # (periodic_report, features.summary, dry_seam_audit) all read the SHARED LIVE ledger, so
        # the read-only half is not optional: rendering a report must never mutate it.
        summary_path = root / "summary-drift.json"
        drifted_gate = _blank_capability("redirect-apply-bootstrap")
        # The real 2026-08-21 shape: `shadow` on disk, `canary` in KNOWN_GATES, reconciled 08:04:50.
        drifted_gate.update({"status": "shadow", "event_history": []})
        assert KNOWN_GATES["redirect-apply-bootstrap"]["status"] == "canary", "fixture drifted"
        # CONTROL: a compiled row is NOT in KNOWN_GATES, so reconciliation cannot touch it and its
        # disk status must survive verbatim. This is why the `create=False` readers in
        # capability_compiler.py need no change — every id it registers is forced to the
        # `capability:` prefix (capability_compiler.py:275 and :1230), a namespace disjoint from
        # KNOWN_GATES' bare slugs — and it is what keeps this fix narrow.
        assert not any(
            k.startswith("capability:") for k in KNOWN_GATES
        ), "KNOWN_GATES took a compiled id; the compiler's create=False readers now need this fix"
        compiled = _blank_capability("capability:reference-sync-hygiene-test-gate")
        compiled.update({"status": "shadow", "event_history": []})
        _write_ledger_unlocked(
            summary_path,
            {
                "redirect-apply-bootstrap": drifted_gate,
                "capability:reference-sync-hygiene-test-gate": compiled,
            },
        )
        summary_before = summary_path.read_bytes()
        raw_counts = summary(summary_path, create=False)
        # READ-ONLY FIRST, so the two halves fail SEPARATELY: reverting to the writing `load()` to
        # get the reconciled view trips this line, and reverting to the raw `load(create=False)`
        # trips the counts below. One assertion catching both would demonstrate neither.
        assert summary_path.read_bytes() == summary_before, "summary(create=False) must not write"
        assert raw_counts["counts_by_status"]["canary"] == 1, raw_counts["counts_by_status"]
        assert raw_counts["counts_by_status"]["shadow"] == 1, raw_counts["counts_by_status"]
        reported = raw_counts["capabilities"]["redirect-apply-bootstrap"]
        assert reported["status"] == "canary", reported["status"]
        # The row the consumers actually print must carry the declared gate fields too, or
        # `classify_liveness` in periodic_report/dry_seam_audit still answers from stale disk text.
        assert reported["matcher"] == KNOWN_GATES["redirect-apply-bootstrap"]["matcher"], reported
        assert reported["gate_reason"] == KNOWN_GATES["redirect-apply-bootstrap"]["gate_reason"]
        assert (
            raw_counts["capabilities"]["capability:reference-sync-hygiene-test-gate"]["status"]
            == "shadow"
        ), "control drifted"
        # ...and the writing path is unchanged: create=True still reconciles ON DISK. (It also
        # registers every other KNOWN_GATES declaration, so assert the ROW, not a global count.)
        persisted = summary(summary_path, create=True)["capabilities"]
        assert persisted["redirect-apply-bootstrap"]["status"] == "canary", persisted
        assert summary_path.read_bytes() != summary_before, "create=True must still persist"
        assert (
            load(summary_path, create=False)["redirect-apply-bootstrap"]["status"] == "canary"
        ), "create=True did not reconcile disk"

        cap = _blank_capability("active-fixture")
        cap.update(
            {
                "status": "active",
                "owner": "orchestrator",
                "matcher": {"kind": "task_type", "equals": "implement"},
                "entrypoint": "worker.py",
                "trigger_cadence": "per dispatch",
                "output_artifact": "result.json",
                "downstream_consumer": "outcomes.py",
                "learning_sink": "feedback.outcomes",
                "activation_evidence": {
                    probe: {"passed": True, "checked_at": 100, "ref": f"probe:{probe}"}
                    for probe in ACTIVE_PROBES
                },
                "last_invocation": 100,
                "last_match": 99,
                "last_success": 100,
                "outcome_links": ["run-1"],
                "expiry": _now() + 3600,
                "kill_switch": "ORCH_FIXTURE=0",
                "rollback": {"transition": "retired"},
            }
        )
        validate_capability(cap)
        missing_entrypoint = dict(cap)
        missing_entrypoint["entrypoint"] = None
        try:
            validate_capability(missing_entrypoint)
            raise AssertionError("active capability missing entrypoint")
        except AssertionError as exc:
            assert "active capability missing entrypoint" in str(exc)

        register("active-fixture", {**cap, "status": "canary"}, ledger)
        heartbeat("active-fixture", "match", timestamp=101, path=ledger)
        heartbeat("active-fixture", "invocation", ref="run-2", timestamp=102, path=ledger)
        heartbeat("active-fixture", "success", ref="run-2", timestamp=103, path=ledger)
        heartbeat("active-fixture", "outcome", ref="run-2", timestamp=104, path=ledger)
        loaded = load(ledger)
        assert loaded["active-fixture"]["last_match"] == 101
        assert loaded["active-fixture"]["last_invocation"] == 102
        assert loaded["active-fixture"]["last_success"] == 103
        assert "run-2" in loaded["active-fixture"]["outcome_links"]

        expiring = loaded["active-fixture"]
        expiring["expiry"] = 105
        save(loaded, ledger)
        assert sweep(ledger, now=105) == ["active-fixture"]
        stored = _read_ledger_unlocked(ledger)["capabilities"]
        assert stored["active-fixture"]["status"] == "retired"

    # --- Layer 1: usage rate, evidence debt, unblock classification (2026-08-09) -------------
    now = 1_800_000_000
    day = 86400

    def _cap(**over):
        base = _blank_capability("fixture")
        base.update(over)
        return base

    # usage_rate counts only events INSIDE the window: a burst last year must not read as current.
    windowed = _cap(
        event_history=[
            {"type": "invocation", "timestamp": now - 2 * day},
            {"type": "invocation", "timestamp": now - 3 * day},
            {"type": "outcome", "timestamp": now - 3 * day},
            {"type": "invocation", "timestamp": now - 400 * day},  # outside the window
        ]
    )
    rate = usage_rate(windowed, now=now, window_days=28)
    assert rate["invocations"] == 2 and rate["outcomes"] == 1, rate
    assert rate["invocations_per_week"] == 0.5, rate  # 2 over 4 weeks
    assert rate["age_days"] == 400, rate  # age spans the FULL history

    # evidence_debt turns "gated" into a number, and flags when volume alone cannot help.
    # Feedable requires BOTH: a threshold code can check (Layer 2) and evidence still short of it.
    gated = _cap(
        status="shadow",
        gate_reason="advisory only",
        gate_criteria={"min_independent_durable_reuse": 3},
        causal_evidence={
            "readiness": {
                "durable_subjects": ["a"],
                "terminal_outcomes": 1,
                "failures": 0,
                "rework": 0,
                "latest_evidence_ts": now - 3600,
                "ready": False,
            }
        },
    )
    debt = evidence_debt(gated)
    assert debt["durable_reuses"] == 1 and debt["required"] == 3 and debt["remaining"] == 2, debt
    assert not debt["blocked_by"], debt
    act = unblock(gated, now=now)
    assert act["feed"] is True and "2 more" in act["blocker"], act  # the one feedable case

    # A recorded failure breaches a zero-tolerance bound: more volume will NOT lift the gate,
    # so the acquisition lane must NOT be told to feed it.
    broken = _cap(
        status="shadow",
        gate_reason="advisory only",
        causal_evidence={
            "readiness": {
                "durable_subjects": ["a", "b", "c"],
                "failures": 1,
                "rework": 0,
                "ready": False,
            }
        },
    )
    assert evidence_debt(broken)["blocked_by"], evidence_debt(broken)
    assert unblock(broken, now=now)["feed"] is False, "failed gates must not be fed"

    # A DEFAULT-OFF SWITCH MUST NOT BE FED EITHER. The sibling assertion above covers a FAILED gate;
    # this covers a gate that is fine but held closed by a documented default-off flag. Feeding it
    # manufactures work the capability cannot execute, so the durable reuse it needs can never be
    # produced and the same capability is fed every cycle forever -- the drain blocked by the very
    # switch the feed ignored. The remaining count must still be REPORTED, so the capability stays
    # visible as evidence-starved rather than disappearing from the queue.
    gated_off = dict(
        gated,
        kill_switch="restore documented default-off gate",
        evidence_threshold={"independent_durable_reuse": 3},
    )
    off = unblock(gated_off, now=now)
    assert off["feed"] is False, ("a documented default-off switch cannot be fed", off)
    assert "default-off switch" in off["blocker"], off
    assert "more independent durable reuse" in off["blocker"], (
        "the evidence debt must still be reported, not hidden",
        off,
    )
    assert "owner decision" in off["action"], off
    # And a capability with the SAME debt but no default-off switch stays feedable, so the guard
    # narrows nothing it should not.
    assert unblock(dict(gated_off, kill_switch="disable via config"), now=now)["feed"] is True

    # Satisfied thresholds surface as READY TO LIFT, and are not fed further.
    ready = _cap(
        status="shadow",
        gate_reason="advisory only",
        gate_criteria={"min_independent_durable_reuse": 3},
        causal_evidence={
            "readiness": {
                "durable_subjects": ["a", "b", "c"],
                "terminal_outcomes": 3,
                "failures": 0,
                "rework": 0,
                "latest_evidence_ts": now - 3600,
                "ready": True,
            }
        },
    )
    assert unblock(ready, now=now)["action"].startswith("READY TO LIFT")
    assert unblock(ready, now=now)["feed"] is False

    # A measurement gap must never be fed — more runs would only add unmeasured runs.
    unmeasured = _cap(status="wired", last_invocation=now - day, last_match=now - 2 * day)
    gap = unblock(unmeasured, now=now)
    assert gap["feed"] is False and "MEASUREMENT gap" in gap["action"], gap

    # Never matched => UNREALIZED (needs a trigger), never a retirement default. Owner direction:
    # the purpose is to maximize capability use, so silence means "not yet wired", not "kill it".
    stale = _cap(
        status="generated", event_history=[{"type": "migrated", "timestamp": now - 200 * day}]
    )
    old = unblock(stale, now=now)
    assert old["needs_trigger"] is True and old["feed"] is False, old
    assert old["retire_candidate"] is False, "long dormancy must not auto-propose retirement"
    assert "still wanted" in old["action"], old  # long-unused still prompts a check
    fresh = _cap(
        status="generated", event_history=[{"type": "migrated", "timestamp": now - 5 * day}]
    )
    young = unblock(fresh, now=now)
    assert young["needs_trigger"] is True and "still wanted" not in young["action"], young
    # A capability that HAS a trigger is not in the enablement queue, even before work matches it.
    wired_trigger = _cap(
        status="generated",
        matcher={"field": "task_type", "operator": "in", "value": ["testgen"]},
        event_history=[{"type": "migrated", "timestamp": now - 200 * day}],
    )
    wt = unblock(wired_trigger, now=now)
    assert wt["needs_trigger"] is False, "a declared trigger must not read as 'needs a trigger'"
    assert "no work has matched" in wt["blocker"], wt
    # Every branch must expose the key so the enablement queue is never silently empty.
    for probe in (stale, fresh, unmeasured, gated, ready, broken):
        assert "needs_trigger" in unblock(probe, now=now), probe.get("status")

    # Running at volume with outcomes => promotable, not a dead-upstream hunt (the abcd-experiment
    # case: 21 invocations/week and 143 outcomes were reported as 'find the dead producer').
    busy = _cap(
        status="generated",
        outcome_links=["r1", "r2"],
        event_history=[{"type": "invocation", "timestamp": now - day}] * 5,
    )
    busy_act = unblock(busy, now=now)
    assert busy_act["blocker"].startswith("running with evidence"), busy_act
    assert busy_act["feed"] is False, busy_act

    # --- Module version ADOPTION: unblock lineage without breaking immutability (2026-08-09) ---
    with tempfile.TemporaryDirectory(prefix="capabilities-adopt-selftest-") as td:
        droot = Path(td)
        ledger = droot / "capabilities.json"
        src = droot / "some_lane.py"
        src.write_text("# v1\n")
        rec = _blank_capability("some-lane")
        rec["status"] = "generated"
        rec["entrypoint"] = "some_lane.py"
        ghostrec = _blank_capability("ghost-lane")
        ghostrec["status"] = "generated"
        ghostrec["entrypoint"] = "Elsewhere/not_here.py"
        save({"some-lane": rec, "ghost-lane": ghostrec}, ledger)

        first = adopt_module_version("some-lane", path=ledger, root=droot)
        assert first["adopted"] and first["capability_version_id"], first
        stored = load(ledger)["some-lane"]
        assert stored["target_kind"] == "module", stored
        assert stored["artifact_hash"] and stored["lifecycle_policy_hash"], stored
        # Idempotent: adopting again changes nothing.
        again = adopt_module_version("some-lane", path=ledger, root=droot)
        assert again["adopted"] is False and again["reason"] == "already current", again
        assert load(ledger)["some-lane"]["capability_version_id"] == first["capability_version_id"]

        # IMMUTABILITY HOLDS: source change is reported as drift, never rewritten in place.
        src.write_text("# v2 — changed\n")
        drift = adopt_module_version("some-lane", path=ledger, root=droot)
        assert drift["adopted"] is False and drift["reason"] == "version_drift", drift
        assert drift["source_version_id"] != drift["current_version_id"], drift
        assert (
            load(ledger)["some-lane"]["capability_version_id"] == first["capability_version_id"]
        ), "drift must not silently rewrite an established version"

        # An unresolvable entrypoint refuses adoption rather than hashing a placeholder.
        ghost = adopt_module_version("ghost-lane", path=ledger, root=droot)
        assert ghost["adopted"] is False and "does not resolve" in ghost["reason"], ghost
        assert load(ledger)["ghost-lane"]["capability_version_id"] is None, ghost

        # Adoption does NOT activate: routing still requires status=='active'.
        adopted_cap = load(ledger)["some-lane"]
        assert adopted_cap["status"] == "generated", "adoption must not promote"
        decision = capability_routing_decision(
            {"repository": "o/r", "task_type": "review", "lane": "opener"},
            capabilities_by_id={
                "some-lane": {
                    **adopted_cap,
                    "matcher": {"field": "task_type", "operator": "in", "value": ["review"]},
                }
            },
            seed=1,
        )
        assert decision["eligible_capability_ids"] == [], "lineage alone must not make it eligible"
        assert decision["rejection_reasons"]["some-lane"] == ["status:generated"], decision

        bulk = adopt_all_module_versions(path=ledger, root=droot)
        assert "ghost-lane" in bulk["skipped"], bulk

    # --- Matcher evaluation FAILS CLOSED (2026-08-09) ----------------------------------------
    trig = {"repository": "o/r", "task_type": "review", "lane": "opener"}
    # The work-routed shape is evaluated exactly.
    assert _matches_trigger(
        {"matcher": {"field": "task_type", "operator": "in", "value": ["review"]}}, trig
    ) == (True, [])
    miss = _matches_trigger(
        {"matcher": {"field": "task_type", "operator": "in", "value": ["codemod"]}}, trig
    )
    assert miss == (False, ["task_type_mismatch"]), miss
    # THE BUG THIS FIXES: shapes the evaluator cannot assess must NOT pass. Each of these used to
    # return (True, []) — matching every trigger in the fleet.
    for matcher, reason in (
        ({"kind": "role", "equals": "triage"}, "role_not_in_trigger"),
        ({"kind": "evidence_gate", "name": "x"}, "evidence_gate_not_in_trigger"),
        (
            {"kind": "env", "name": "ORCH_DEFINITELY_UNSET", "equals": "1"},
            "env_mismatch:ORCH_DEFINITELY_UNSET",
        ),
        ({"kind": "evidence_gate"}, "matcher_kind_missing_expected_value:evidence_gate"),
        ({"field": "nonexistent_field", "value": ["x"]}, "unknown_matcher_field:nonexistent_field"),
        ({"totally_unknown_key": "x"}, "unknown_matcher_key:totally_unknown_key"),
    ):
        matched, reasons = _matches_trigger({"matcher": matcher}, trig)
        assert matched is False and reasons == [reason], (matcher, matched, reasons)
    # GENERALISED KINDS: a kind matches a same-named trigger field, so adding a trigger kind is a
    # caller-side change. Each of these was permanently unmatchable before 2026-08-09.
    for kind, expected_key, value in (
        ("feedback_event", "name", "record_run"),
        ("evidence_gate", "name", "ready_for_supervised_apply"),
        ("supervised_trial", "name", "sol-terra-luna"),
        ("experiment_phase", "equals", "evaluated"),
        ("tick_phase", "name", "capacity"),
        ("transport", "name", "offload"),
    ):
        m = {"kind": kind, expected_key: value}
        assert _matches_trigger({"matcher": m}, {**trig, kind: value}) == (True, []), m
        miss = _matches_trigger({"matcher": m}, {**trig, kind: "something-else"})
        assert miss == (False, [f"{kind}_mismatch"]), (m, miss)
        # Absent context is still a non-match, never a pass.
        assert _matches_trigger({"matcher": m}, trig) == (False, [f"{kind}_not_in_trigger"]), m
    # No matcher means nothing can route here — not "everything routes here".
    assert _matches_trigger({}, trig) == (False, ["no_matcher_declared"])
    # A role matcher DOES match once the trigger carries the role.
    assert _matches_trigger(
        {"matcher": {"kind": "role", "equals": "triage"}}, dict(trig, role="triage")
    ) == (True, [])
    # Routing must not become eligible purely because a matcher is unevaluatable.
    decision = capability_routing_decision(
        trig,
        capabilities_by_id={
            "ghost": {
                **_blank_capability("ghost"),
                "status": "active",
                "matcher": {"kind": "evidence_gate", "name": "x"},
                "capability_version_id": "v1",
                "artifact_hash": "h",
                "lifecycle_policy_hash": "p",
            }
        },
        seed=1,
    )
    assert decision["eligible_capability_ids"] == [], decision
    assert decision["rejection_reasons"]["ghost"] == ["evidence_gate_not_in_trigger"], decision

    # --- Layer 2: gate readiness for ANY status, silence never passes (2026-08-09) -----------
    def _gate(readiness=None, **over):
        cap = _cap(
            status="shadow",
            gate_reason="advisory only",
            evidence_threshold="five linked outcomes, three durable",
            **over,
        )
        cap["causal_evidence"] = {"readiness": readiness or {}}
        return cap

    # An UNGATED capability is simply not gated — not "ready".
    assert gate_readiness(_cap(status="shadow"), now=now) == {
        "gated": False,
        "ready": False,
        "criteria": {},
        "unevaluated": [],
        "reason": "not gated",
    }

    # Prose-only threshold: evaluable evidence exists, but nothing encoded it => NEVER ready.
    prose = _gate(
        {
            "durable_subjects": ["a", "b", "c"],
            "terminal_outcomes": 9,
            "failures": 0,
            "rework": 0,
            "latest_evidence_ts": now - 3600,
        }
    )
    pg = gate_readiness(prose, now=now)
    assert pg["gated"] and not pg["ready"] and not pg["encoded"], pg
    assert "prose only" in pg["reason"], pg
    assert unblock(prose, now=now)["feed"] is False, "an uncheckable gate must not be fed"

    # Encoded + satisfied => ready.
    enc = _gate(
        {
            "durable_subjects": ["a", "b", "c"],
            "terminal_outcomes": 5,
            "failures": 0,
            "rework": 0,
            "latest_evidence_ts": now - 3600,
        },
        gate_criteria={"min_linked_outcomes": 5, "min_independent_durable_reuse": 3},
    )
    eg = gate_readiness(enc, now=now)
    assert eg["encoded"] and eg["ready"], eg
    assert unblock(enc, now=now)["action"].startswith("READY TO LIFT")

    # Encoded but one bound unmet => not ready, and the SPECIFIC bound is named.
    short = _gate(
        {
            "durable_subjects": ["a", "b", "c"],
            "terminal_outcomes": 2,
            "failures": 0,
            "rework": 0,
            "latest_evidence_ts": now - 3600,
        },
        gate_criteria={"min_linked_outcomes": 5, "min_independent_durable_reuse": 3},
    )
    sg = gate_readiness(short, now=now)
    assert not sg["ready"] and sg["unmet"] == ["linked_outcomes"], sg

    # A criterion the causal record cannot supply keeps the gate un-auto-liftable BY DESIGN,
    # even when every countable bound is already satisfied.
    ext = _gate(
        {
            "durable_subjects": ["a", "b", "c"],
            "terminal_outcomes": 9,
            "failures": 0,
            "rework": 0,
            "latest_evidence_ts": now - 3600,
        },
        gate_criteria={
            "min_independent_durable_reuse": 3,
            "requires": ["exploration_review_recommendation"],
        },
    )
    xg = gate_readiness(ext, now=now)
    assert xg["encoded"] and not xg["ready"] and xg["unevaluated"], xg
    assert "exploration_review_recommendation" in xg["unevaluated"][0], xg
    assert unblock(ext, now=now)["feed"] is False, "un-evaluable gates must not be fed"

    # An unrecognised criterion must not be silently ignored into a pass.
    bogus = _gate(
        {
            "durable_subjects": ["a", "b", "c"],
            "terminal_outcomes": 9,
            "failures": 0,
            "rework": 0,
            "latest_evidence_ts": now - 3600,
        },
        gate_criteria={"min_independent_durable_reuse": 3, "vibes_are_good": True},
    )
    assert not gate_readiness(bogus, now=now)["ready"], "unknown criteria must block readiness"

    # Gate readiness is status-independent: the same evidence reads the same on wired/canary.
    for status in ("wired", "canary", "shadow"):
        variant = dict(enc, status=status)
        assert gate_readiness(variant, now=now)["ready"], status

    report = usage_report(
        {
            "capabilities": {
                "g": gated,
                "r": ready,
                "u": unmeasured,
                "s": stale,
                "p": prose,
                "e": enc,
            }
        },
        now=now,
    )
    assert report["ready_to_lift"] == ["e", "r"] and report["worth_feeding"] == ["g"], report
    assert report["measurement_gaps"] == ["u"] and report["needs_trigger"] == ["s"], report
    assert report["retire_candidates"] == [], "dormancy must never auto-propose retirement"
    assert report["threshold_undefined"] == ["p"], report
    assert sorted(report["gate_encoded"]) == ["e", "g", "r"], report
    text_rl = format_usage_report(report)
    # A frequently-invoked capability WITH outcome evidence must not read as a measurement gap
    # just because its newest invocation postdates its newest outcome (outcomes lag by design).
    lag = _blank_capability("lagging")
    lag["status"] = "shadow"
    lag["last_match"] = 1000
    lag["last_invocation"] = 9_000_000  # invoked just now
    lag["outcome_links"] = ["run-a", "run-b"]  # ...and it HAS linked outcomes
    lag["event_history"] = [{"type": "outcome", "timestamp": 5_000_000}]
    assert classify_liveness(lag) != "invoked_without_outcomes", classify_liveness(lag)
    # With NO outcome evidence at all it correctly still reports the gap.
    dry = dict(lag, outcome_links=[], event_history=[])
    assert classify_liveness(dry) == "invoked_without_outcomes", classify_liveness(dry)

    assert "READY TO LIFT" in text_rl
    # The zero must carry its reachable denominator, so an immovable 0 cannot read as a failure.
    assert "machine-checkable" in text_rl, text_rl[:400]
    assert "prose no code can check" in text_rl
    if report.get("threshold_undefined"):
        assert f"reachable for {len(report['gate_encoded'])} of" in text_rl, text_rl[:600]
    # With every gate encoded, the report must NOT print "the other 0 state their threshold in
    # prose" — a zero-count sentence that reads as nonsense. It must say what actually holds them.
    for cap in report["rows"]:
        cap.setdefault("gate", {})
    if not report.get("threshold_undefined"):
        assert "the other 0 state" not in text_rl, text_rl[:400]

    # ---- ATTRIBUTABLE-FAILURE TALLY (added 2026-09-01) -----------------------------------
    # The measured defect: 96 remote keepalive PRs closed unmerged (blanket adjudicated
    # FAIL/abandoned, failure_class=None) plus 8 advisory self-edges trained role-triage's
    # routing prior to ~1/106 and pinned its gate at "104 failure(s) over the max of 0" —
    # a permanently unsatisfiable promotion gate fed by non-failures. Both consumers
    # (_causal_readiness and reconcile_causal_lifecycle) now count feedback's tally_class,
    # the single per-row classification, so they cannot drift apart.
    def _mk_row(tally, *, subject="s", rework=False, ts=1_700_000_000):
        return {
            "edge_id": f"e-{tally}-{subject}",
            "subject_id": subject,
            "target_run_id": f"run-{subject}",
            "target_event_id": f"event-{subject}",
            "source_event_id": f"src-event-{subject}",
            "source_run_id": f"src-run-{subject}",
            "merged": None,
            "failure_class": None,
            "cost": {},
            "accepted_consumption": tally != "",
            "counterfactual": False,
            "outcome_verdict": None,
            "durability": None,
            "profile_attempt_ids": [],
            "terminal_outcome": tally in ("success", "failure", "churn_unattributed"),
            "durable_success": tally == "success",
            "tally_class": tally,
            "rework": rework,
            "regression": False,
            "observed_ts": ts,
            "acceptance_gate_id": None,
        }

    synthetic = [
        _mk_row("success", subject="s1"),
        _mk_row("success", subject="s2"),
        _mk_row("failure", subject="s3"),
        _mk_row("churn_unattributed", subject="s4"),
        _mk_row("churn_unattributed", subject="s5"),
        _mk_row("churn_unattributed", subject="s6"),
        _mk_row("advisory_self", subject="s7"),
        _mk_row("", subject="s8"),
    ]
    fake_cap: dict[str, Any] = {"lifecycle_policy": {}}
    ready = _causal_readiness(fake_cap, synthetic, 1_700_000_100)
    assert ready["failures"] == 1, (
        "only the attributable row may count as failure; churn counted again would recreate "
        f"the 104-failure artifact: {ready['failures']}"
    )
    assert ready["churn_unattributed"] == 3, ready
    assert ready["advisory_self_edges"] == 1, ready
    # advisory_self rows are accepted consumptions but not terminal tallies
    assert ready["accepted_consumptions"] == 7 and ready["terminal_outcomes"] == 6, ready
    # the excluded classes are REPORTED — a tally that cannot say what it declined to count
    # is the silent no-op class again
    for key in ("churn_unattributed", "advisory_self_edges"):
        assert key in ready, f"exclusion {key} must be visible in readiness"
    # the naming convention feedback's advisory_self relies on (capability role-<name> for a
    # role run named <name>) is pinned against the in-code offer table, not assumed:
    import capability_advisor as _ca
    import roles as _roles

    _role_ids = {f"role-{name}" for name in _roles.ROLE_REGISTRY}
    _known = set(_ca.HOW_TO_USE)
    assert _role_ids <= _known, (
        "role registry and role-<name> capability ids have diverged; feedback.advisory_self "
        f"relies on this convention: missing {sorted(_role_ids - _known)}"
    )

    # rollback_pending drains with its own evidence: set by a regression row, cleared —
    # with a NAMED event — when a later review of the same lineage finds none, because the
    # only other clear path requires a routable predecessor and the flag rejects routing
    # while it stands (the one-way-latch shape, measured live on role-triage).
    import tempfile as _tf

    import feedback as _fb

    with _tf.TemporaryDirectory(prefix="rollback-drain-selftest-") as _td:
        _ledger = Path(_td) / "capabilities.json"
        _probe = _blank_capability("latch-probe")
        _probe["status"] = "shadow"
        _probe["capability_version_id"] = "capability-version:latchprobe"
        _probe["artifact_hash"] = "sha256:latchprobe"
        _probe["lifecycle_policy_hash"] = "sha256:latchprobe"
        save({"latch-probe": _probe}, _ledger)

        def _regressing(*_a, **_k):
            row = _mk_row("failure", subject="g1")
            row["regression"] = True
            return [row]

        def _drained(*_a, **_k):
            return [_mk_row("churn_unattributed", subject="g1")]

        _orig_evidence = _fb.capability_causal_evidence
        try:
            _fb.capability_causal_evidence = _regressing
            first = reconcile_causal_lifecycle("latch-probe", path=_ledger, timestamp=1_700_000_200)
            assert first.get("rollback_pending"), "a regression row must set the pending flag"
            _fb.capability_causal_evidence = _drained
            second = reconcile_causal_lifecycle(
                "latch-probe", path=_ledger, timestamp=1_700_000_300
            )
            assert not second.get("rollback_pending"), (
                "pending must drain when its own regression evidence does — a flag only a "
                "nonexistent predecessor can clear is the one-way latch this exists to kill: "
                + str(second.get("rollback_pending"))
            )
            _stored = load(_ledger, create=False)["latch-probe"]
            _cleared = [
                e for e in _stored["event_history"] if e["type"] == "rollback_pending_cleared"
            ]
            assert (
                _cleared and "reclassification" in _cleared[-1]["reason"]
            ), "the clear must be a NAMED event, never a silent field wipe: " + str(_cleared)
        finally:
            _fb.capability_causal_evidence = _orig_evidence

    print(
        "capabilities.py rollback-drain selftest: OK (pending set by regression evidence "
        "drains with it, through a named event, and a pending written by other machinery "
        "is left alone)"
    )
    print(
        "capabilities.py attributable-failure selftest: OK (churn and advisory self-edges are "
        "excluded from alpha/beta and named in the readiness view, one tally_class feeds both "
        "consumers, and the role-id naming convention is pinned)"
    )
    print(
        "capabilities.py selftest: OK (+ usage rate / evidence debt / unblock classification, "
        "gate readiness w/ never-pass-on-silence)"
    )


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        _selftest()
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("validate")
    sub.add_parser("summary")
    sub.add_parser("inventory")
    sub.add_parser("usage")  # why capabilities are NOT used, and what would change that
    sub.add_parser("sweep")
    sub.add_parser("reconcile")
    register_cmd = sub.add_parser("register")
    register_cmd.add_argument("--name", required=True)
    register_cmd.add_argument("--record-json", type=Path, required=True)
    transition_cmd = sub.add_parser("transition")
    transition_cmd.add_argument("--name", required=True)
    transition_cmd.add_argument("--to", required=True, choices=CANONICAL_STATES)
    transition_cmd.add_argument("--reason", required=True)
    transition_cmd.add_argument("--evidence-ref", action="append", default=[])
    probe_cmd = sub.add_parser("probe")
    probe_cmd.add_argument("--name", required=True)
    probe_cmd.add_argument("--probe", required=True, choices=ACTIVE_PROBES)
    probe_result = probe_cmd.add_mutually_exclusive_group(required=True)
    probe_result.add_argument("--passed", action="store_true")
    probe_result.add_argument("--failed", action="store_true")
    probe_cmd.add_argument("--ref", required=True)
    probe_cmd.add_argument("--detail")
    event = sub.add_parser("heartbeat")
    event.add_argument("--name", required=True)
    event.add_argument("--type", required=True, choices=tuple(EVENT_FIELDS))
    event.add_argument("--ref")
    args = parser.parse_args(argv)
    if args.command == "validate":
        result = validate_ledger()
        print(
            json.dumps(result, indent=2)
            if args.json
            else ("PASS" if result["valid"] else json.dumps(result, indent=2))
        )
        return 0 if result["valid"] else 1
    if args.command == "summary":
        print(json.dumps(summary(), indent=2))
        return 0
    if args.command == "inventory":
        report = summary()
        print(json.dumps(report, indent=2) if args.json else format_inventory(report), end="")
        return 0
    if args.command == "usage":
        usage = usage_report(summary())
        print(json.dumps(usage, indent=2) if args.json else format_usage_report(usage), end="")
        return 0
    if args.command == "sweep":
        result = {"retired": sweep()}
        print(json.dumps(result, indent=2))
        return 0
    if args.command == "reconcile":
        result = reconcile_all()
        print(json.dumps(result, indent=2))
        return 0 if result["valid"] else 1
    if args.command == "register":
        record = json.loads(args.record_json.read_text(encoding="utf-8"))
        register(args.name, record)
        return 0
    if args.command == "transition":
        transition(
            args.name,
            args.to,
            reason=args.reason,
            evidence_refs=args.evidence_ref,
        )
        return 0
    if args.command == "probe":
        record_probe(
            args.name,
            args.probe,
            passed=args.passed and not args.failed,
            ref=args.ref,
            detail=args.detail,
        )
        return 0
    if args.command == "heartbeat":
        heartbeat(args.name, args.type, ref=args.ref)
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
