#!/usr/bin/env python3
"""Deterministic independent-subject capability pattern miner.

Input is the storage-independent v1 envelope defined in
``completion_event_adapter.py``.  This module assembles phase events into
episodes, normalizes their execution graph, applies deterministic evidence
gates, and emits candidate-only ``CapabilityIR`` records.  It never generates
or activates code, skills, roles, workflows, playbooks, or acceptance gates.

Cadence integration is a pipe plus an atomic state artifact, not a database
dependency::

    <joined-completion-event-exporter> --jsonl |
      python3 pattern_miner.py run --events - \
        --state "$ORCH_STATE_DIR/pattern-miner-state.json" \
        --status-out "$ORCH_STATE_DIR/pattern-miner-status.json" \
        --inventory-out "$ORCH_STATE_DIR/pattern-miner-inventory.json"

The exporter supplies a complete safe-envelope snapshot. The runner restores
candidate/tombstone lifecycle state before mining, so an empty cadence can
still expire candidates and retain tombstones. All writes are atomic and all
outputs are machine-readable JSON; none creates a human review queue.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass, replace
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

from capability_ir import (
    CapabilityIR,
    CandidateTombstone,
    Counterexample,
    Lifecycle,
    SourceOccurrence,
    stable_hash,
)
from completion_event_adapter import (
    CompletionEvent,
    EnvelopeError,
    OutOfScopeError,
    PHASES,
    adapt_completion_event_envelope,
)


STATUS_SCHEMA = "orchestrator.pattern-miner-status"
STATUS_VERSION = 1
STATE_SCHEMA = "orchestrator.pattern-miner-state"
STATE_VERSION = 1
DEFAULT_MIN_POSITIVE_SUBJECTS = 3
DEFAULT_MAX_NEGATIVE_RATIO = 0.5
DEFAULT_CANDIDATE_TTL_DAYS = 30
POSITIVE_VERDICTS = {"pass", "passed", "success", "successful"}
DURABLE_STATUSES = {"durable", "held", "survived"}
TERMINAL_FAILURE_DURABILITY = {"broke_later", "reopened", "reverted", "reworked"}


# A systemic input fault rejects every event, so the per-event detail is capped in
# the emitted artifact while the aggregate below stays complete. Without the cap a
# corpus-wide fault trades silence for a status file the size of the corpus, echoed
# into the cadence log on every tick.
MAX_REPORTED_REJECTIONS = 200


@dataclass(frozen=True)
class Rejection:
    event_id: str
    phase: str
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _producer_counts(rejections: Iterable["Rejection"]) -> dict[str, int]:
    """Count excluded events per producer, read off the `no_research_subject:<producer>` reason.

    Names WHICH producers the stream is made of, so "excluded 1784" is attributable instead of
    anonymous. An exclusion count that suddenly moves to a producer declared subject-capable is a
    registration gap worth chasing; one that stays on the declared-subjectless producers is normal.
    """
    counts: Counter[str] = Counter()
    for rejection in rejections:
        for reason in rejection.reasons:
            if reason.startswith("no_research_subject:"):
                counts[reason.split(":", 1)[1] or "unknown"] += 1
    return dict(sorted(counts.items()))


def _mining_health(
    raw: int,
    accepted: int,
    rejected: int,
    excluded: int,
    episodes: int,
    candidates: int,
    reasons: dict[str, int] | None = None,
) -> dict[str, Any]:
    """One machine-readable verdict a cadence step can branch on, instead of an exit code.

    Four states, deliberately distinguishable:

    * ``no_input``       - nothing was exported. The 2026-07-10 "success" was this, and the stamp
                           recorded it as healthy, which is why the miner looked fine having never
                           mined anything.
    * ``all_out_of_scope`` - the stream ran clean but carried no research subject at all. Expected
                           for a production-only corpus; NOT a fault, and not success either.
    * ``rejecting``      - real defects in the stream. Actionable.
    * ``mining``         - episodes assembled.
    """
    # HOW MANY OF THESE COULD EVER BE FIXED. `missing_joined_attempt_id` means the event carries no
    # attempt row to join, and the export builds that join FROM the attempt row -- so if the run
    # finished without one, nothing downstream can ever supply it. Those rejections are history, not
    # a queue. Verified for the live population: 25 UX-panel subjects, 0 with a base commit, 0
    # execution attempts, all runs 2026-07-16..08-16, i.e. before provenance existed. Reporting the
    # count keeps `rejecting` honest -- "184 malformed" reads as work somebody should do, and a
    # rejection nobody can act on is the same silence a bare count of blocked items always is.
    unrecoverable = int((reasons or {}).get("missing_joined_attempt_id") or 0)
    ranked = sorted((reasons or {}).items(), key=lambda kv: (-kv[1], kv[0]))
    top = ranked[0] if ranked else None
    # A TIE AT THE TOP IS THE WHOLE STORY. Reporting a single "top blocker" when four reasons each
    # affect 100% of the population invites the reader to fix one and expect the queue to drain --
    # it will not move at all, because the other three still block every event. Report the tied
    # set, so the line states how many independent fixes stand between here and one episode.
    tied = [code for code, count in ranked if top and count == top[1]][:4] if top else []
    if raw == 0:
        state, detail = "no_input", "exporter produced no events"
    elif rejected:
        # NAME THE BLOCKER, not just the count. "203 of 12805 rejected as malformed" tells an
        # operator that something is wrong and nothing about what to fix; they have to open a JSON
        # to discover that 200 of those 203 share ONE cause. When the dominant reason accounts for
        # most of the population, one fix drains nearly all of it -- so the count and the cause
        # belong on the same line, which is the drainable-quantity rule applied to a reason code.
        state = "rejecting"
        detail = f"{rejected} of {raw} events rejected as malformed"
        if top and len(tied) > 1:
            detail += f"; {len(tied)} blockers each hit {top[1]} of {rejected}: " + ", ".join(tied)
        elif top:
            detail += f"; top blocker {top[0]} ({top[1]} of {rejected})"
        if unrecoverable:
            drainable = rejected - unrecoverable
            detail += (
                f"; {unrecoverable} have no attempt row to join and can never be repaired, "
                f"{drainable} drainable"
            )
    elif accepted == 0:
        state, detail = "all_out_of_scope", f"all {raw} events have no research subject"
    elif episodes == 0:
        state, detail = "accepted_no_episodes", f"{accepted} events accepted, no complete episode"
    else:
        state, detail = "mining", f"{episodes} episodes, {candidates} candidates"
    return {
        "state": state,
        "detail": detail,
        "raw_event_count": raw,
        "accepted_event_count": accepted,
        "rejected_event_count": rejected,
        "excluded_event_count": excluded,
        "complete_episode_count": episodes,
        "candidate_count": candidates,
        # A cadence step should treat only `rejecting` and `no_input` as unhealthy: the others mean
        # the miner did its job on the input it was given.
        "actionable": state in {"rejecting", "no_input"},
        "summary": f"accepted {accepted} / rejected {rejected} / excluded {excluded} of {raw}",
        # Machine-readable twin of the `detail` suffix, so a consumer branches on the code
        # instead of parsing prose.
        "top_blocker": top[0] if top else None,
        "top_blocker_count": top[1] if top else 0,
        "top_blockers": tied,
        # The pair, machine-readable: what is blocked, and how much of it is actually actionable.
        "unrecoverable_rejections": unrecoverable,
        "drainable_rejections": max(0, rejected - unrecoverable),
    }


def _reason_counts(rejections: Iterable["Rejection"]) -> dict[str, int]:
    """Count rejected events per stable reason code.

    Grouped on the code before the first ``:`` so detail-bearing reasons
    (``missing_phase:trigger``, ``invalid_artifact_ref:3``) cannot make the
    summary unbounded. This is what makes a systemic input fault legible:
    ``accepted_event_count 0`` alone reads as a quiet cadence, whereas
    ``accepted 0 / skipped N / invalid_normalized_spec_hash N`` names the cause on
    sight. Reported beside the counts it explains, per the runtime rule
    that a gate must publish its blocking quantity and its drainable quantity in
    the same place.
    """
    counts: Counter[str] = Counter()
    for rejection in rejections:
        for code in {reason.split(":", 1)[0] for reason in rejection.reasons}:
            counts[code] += 1
    return dict(sorted(counts.items()))


@dataclass(frozen=True)
class NormalizedEpisode:
    episode_id: str
    events: tuple[CompletionEvent, ...]
    graph: dict[str, Any]
    selector: dict[str, Any]
    kind_proposal: str
    owner_proposal: str
    fingerprint: str
    semantic_fingerprint: str
    output_contract_fingerprint: str
    successor_of: tuple[str, ...]
    positive: bool
    evidence_reason: str
    artifact_refs: tuple[str, ...]
    verification_ref: str | None
    outcome_ref: str | None
    durability_ref: str | None

    @property
    def identity(self):
        return self.events[0].identity

    @property
    def occurred_at(self) -> int:
        return max(event.occurred_at for event in self.events)

    @property
    def event_ids(self) -> tuple[str, ...]:
        return tuple(event.event_id for event in self.events)


@dataclass(frozen=True)
class MiningResult:
    candidates: tuple[CapabilityIR, ...]
    rejections: tuple[Rejection, ...]
    tombstones: tuple[CandidateTombstone, ...]
    status: dict[str, Any]

    def to_dict(self, *, include_candidates: bool = True) -> dict[str, Any]:
        payload = dict(self.status)
        payload["rejections"] = [
            item.to_dict() for item in self.rejections[:MAX_REPORTED_REJECTIONS]
        ]
        # Never a silent cap: say what was dropped, so a truncated sample cannot
        # be mistaken for the whole rejection set.
        if len(self.rejections) > MAX_REPORTED_REJECTIONS:
            payload["rejections_truncated"] = {
                "reported": MAX_REPORTED_REJECTIONS,
                "total": len(self.rejections),
                "note": "full per-code totals in rejected_event_reasons",
            }
        payload["tombstones"] = [item.to_dict() for item in self.tombstones]
        if include_candidates:
            payload["candidates"] = [item.to_dict() for item in self.candidates]
        return payload


def _norm_string(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _norm_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(sorted({_norm_string(item) for item in value if _norm_string(item)}))


def _result(event: CompletionEvent) -> dict[str, Any]:
    value = event.payload.get("result")
    return value if isinstance(value, dict) else {}


def _payload_ids(event: CompletionEvent, key: str) -> tuple[str, ...]:
    return _norm_list(event.payload.get(key))


def _derive_kind(decision: CompletionEvent) -> str:
    if _payload_ids(decision, "role_ids"):
        return "role"
    if _payload_ids(decision, "workflow_ids"):
        return "workflow"
    if _payload_ids(decision, "skill_ids"):
        return "skill"
    if _payload_ids(decision, "acceptance_gate_ids"):
        return "acceptance_gate"
    return "unknown"


def _derive_owner(decision: CompletionEvent) -> str:
    if decision.producer.startswith("workflow"):
        return "workflows"
    if decision.producer:
        return "orchestrator"
    return "unknown"


def _artifact_shape(ref: dict[str, Any]) -> str:
    """Retain the bounded artifact contract without content or target names."""
    return f"{_norm_string(ref.get('kind'))}:{_norm_string(ref.get('ref_class'))}"


def normalize_episode(events: dict[str, CompletionEvent]) -> NormalizedEpisode:
    """Normalize one complete trigger-to-durability event DAG."""
    trigger = events["trigger"]
    decision = events["decision"]
    execution = events["execution"]
    artifact = events["artifact"]
    verification = events["verification"]
    outcome = events["outcome"]
    durability = events["durability"]
    identity = trigger.identity

    selector = {"task_type": identity.task_type}
    selected_ids = {
        key: _payload_ids(decision, key)
        for key in (
            "capability_ids",
            "role_ids",
            "skill_ids",
            "workflow_ids",
            "acceptance_gate_ids",
        )
        if _payload_ids(decision, key)
    }
    artifact_records = tuple(artifact.payload.get("artifact_refs") or [])
    artifact_refs = tuple(
        sorted(
            {
                _norm_string(ref.get("artifact_id"))
                for ref in artifact_records
                if isinstance(ref, dict) and _norm_string(ref.get("artifact_id"))
            }
        )
    )
    result_hashes = _payload_ids(artifact, "result_hashes")
    changed_classes = _payload_ids(artifact, "changed_path_classes")
    output_contract = "+".join(changed_classes) or "+".join(
        sorted({_artifact_shape(ref) for ref in artifact_records})
    )
    artifact_kinds = tuple(
        sorted(
            {
                _norm_string(ref.get("kind"))
                for ref in artifact_records
                if isinstance(ref, dict) and _norm_string(ref.get("kind"))
            }
        )
    )
    artifact_kind = "+".join(artifact_kinds) or "artifact"

    verification_payload = verification.payload.get("verification")
    verification_payload = verification_payload if isinstance(verification_payload, dict) else {}
    gate_ids = _payload_ids(verification, "acceptance_gate_ids")
    test_ids = _payload_ids(verification, "test_ids")
    verification_name = _norm_string(verification_payload.get("name")) or (
        gate_ids[0] if gate_ids else test_ids[0] if test_ids else ""
    )
    verification_verdict = _norm_string(
        verification_payload.get("adjudicated_verdict")
        or verification_payload.get("verifier_verdict")
        or verification.status
    )
    verification_hashes = _payload_ids(verification, "result_hashes")
    verification_ref = verification_hashes[0] if verification_hashes else None

    delivery = outcome.payload.get("delivery")
    delivery = delivery if isinstance(delivery, dict) else {}
    outcome_result = _result(outcome)
    outcome_verdict = _norm_string(outcome_result.get("outcome_verdict")) or (
        "success" if delivery.get("merged") is True else outcome.status
    )
    outcome_ref = (
        f"pr:{delivery.get('pr_number')}"
        if delivery.get("pr_number")
        else _norm_string(outcome_result.get("influenced_run_id")) or None
    )

    durability_payload = durability.payload.get("durability")
    durability_payload = durability_payload if isinstance(durability_payload, dict) else {}
    durability_status = _norm_string(durability_payload.get("status")) or durability.status
    durability_ref = (
        f"durability:{int(durability_payload['checked_ts'])}"
        if durability_payload.get("checked_ts") is not None
        else None
    )

    graph = {
        "phase_order": PHASES,
        "edges": tuple(
            {"from": left, "to": right}
            for left, right in zip(PHASES, PHASES[1:])
        ),
        "trigger": {
            "kind": "task_completion",
            "signature": identity.task_type,
        },
        "decision": {
            "kind": _derive_kind(decision),
            "signature": selected_ids
            or {
                "action_id": _norm_string(_result(decision).get("action_id")),
                "decision_source_id": _norm_string(
                    _result(decision).get("decision_source_id")
                ),
            },
            "selected_ids": selected_ids,
        },
        "execution": {
            "operation": _norm_string(_result(execution).get("operation_role"))
            or execution.event_type,
            "entrypoint": execution.producer,
        },
        "artifact": {
            "kind": artifact_kind,
            "output_contract": output_contract,
            "ref_shapes": tuple(sorted({_artifact_shape(ref) for ref in artifact_records})),
            "changed_path_classes": changed_classes,
        },
        "verification": {
            "name": verification_name,
            "command_ids": _payload_ids(verification, "command_ids"),
            "test_ids": test_ids,
        },
        "outcome": {"contract": "accepted_delivery_or_result"},
        "durability": {"contract": "durable_result"},
    }
    semantic_graph = {
        **graph,
        "execution": {"operation": graph["execution"]["operation"]},
    }
    output_graph = {
        "trigger": graph["trigger"],
        "artifact": graph["artifact"],
        "verification": graph["verification"],
        "outcome": graph["outcome"],
    }
    artifact_accepted = artifact.status in {
        "accepted",
        "complete",
        "completed",
        "merged",
        "pass",
        "passed",
        "success",
        "successful",
    } and bool(artifact_refs or result_hashes)
    verification_passed = verification_verdict in POSITIVE_VERDICTS
    outcome_passed = outcome_verdict in POSITIVE_VERDICTS
    durability_passed = durability_status in DURABLE_STATUSES
    if not output_contract:
        evidence_reason = "missing_output_contract"
    elif not verification_name:
        evidence_reason = "unnamed_verification"
    elif not artifact_accepted:
        evidence_reason = "artifact_not_accepted"
    elif not verification_passed:
        evidence_reason = "verification_not_passed"
    elif not outcome_passed:
        evidence_reason = "outcome_not_successful"
    elif not durability_passed:
        evidence_reason = (
            "durability_regressed"
            if durability_status in TERMINAL_FAILURE_DURABILITY
            else "no_durable_result"
        )
    else:
        evidence_reason = "eligible_durable_positive"

    decision_result = _result(decision)
    influence_type = _norm_string(decision_result.get("influence_type"))
    influence_id = _norm_string(decision_result.get("influence_id"))
    successor_of = (
        (influence_id,)
        if influence_type in {"predecessor", "successor_of", "supersedes"}
        and influence_id.startswith("sha256:")
        else ()
    )
    return NormalizedEpisode(
        episode_id=f"{trigger.run_id}:{trigger.attempt_id}",
        events=tuple(events[phase] for phase in PHASES),
        graph=graph,
        selector=selector,
        kind_proposal=_derive_kind(decision),
        owner_proposal=_derive_owner(decision),
        fingerprint=stable_hash("episode-graph-v1", graph),
        semantic_fingerprint=stable_hash("episode-semantic-graph-v1", semantic_graph),
        output_contract_fingerprint=stable_hash("episode-output-contract-v1", output_graph),
        successor_of=successor_of,
        positive=evidence_reason == "eligible_durable_positive",
        evidence_reason=evidence_reason,
        artifact_refs=artifact_refs,
        verification_ref=verification_ref,
        outcome_ref=outcome_ref,
        durability_ref=durability_ref,
    )


class _UnionFind:
    def __init__(self, size: int):
        self.parent = list(range(size))

    def find(self, index: int) -> int:
        while self.parent[index] != index:
            self.parent[index] = self.parent[self.parent[index]]
            index = self.parent[index]
        return index

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parent[max(left_root, right_root)] = min(left_root, right_root)


class PatternMiner:
    def __init__(
        self,
        *,
        min_positive_subjects: int = DEFAULT_MIN_POSITIVE_SUBJECTS,
        max_negative_ratio: float = DEFAULT_MAX_NEGATIVE_RATIO,
        candidate_ttl_days: int = DEFAULT_CANDIDATE_TTL_DAYS,
    ):
        if min_positive_subjects < 1:
            raise ValueError("min_positive_subjects must be positive")
        if not 0 <= max_negative_ratio <= 1:
            raise ValueError("max_negative_ratio must be between zero and one")
        if candidate_ttl_days < 1:
            raise ValueError("candidate_ttl_days must be positive")
        self.min_positive_subjects = min_positive_subjects
        self.max_negative_ratio = max_negative_ratio
        self.ttl_seconds = candidate_ttl_days * 86400
        self.candidates: list[CapabilityIR] = []
        self.tombstones: list[CandidateTombstone] = []
        self.rejections: list[Rejection] = []
        self.exclusions: list[Rejection] = []
        self._status: dict[str, Any] = {}
        self.state_loaded = False

    def load_state(self, path: Path) -> bool:
        """Load durable candidate lifecycle state; a missing file is first-run state."""
        if not path.exists():
            self.state_loaded = False
            return False
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema") != STATE_SCHEMA or payload.get("version") != STATE_VERSION:
            raise ValueError("unsupported pattern miner state schema")
        self.candidates = [
            CapabilityIR.from_dict(item) for item in payload.get("candidates") or ()
        ]
        self.tombstones = [
            CandidateTombstone.from_dict(item)
            for item in payload.get("tombstones") or ()
        ]
        self._status = dict(payload.get("last_status") or {})
        self.state_loaded = True
        return True

    def state_dict(self, *, updated_at: int | None = None) -> dict[str, Any]:
        """Return the versioned state artifact owned by the cadence runner."""
        return {
            "schema": STATE_SCHEMA,
            "version": STATE_VERSION,
            "updated_at": int(time.time()) if updated_at is None else int(updated_at),
            "input_mode": "completion_event_snapshot",
            "candidates": [item.to_dict() for item in self.candidates],
            "tombstones": [item.to_dict() for item in self.tombstones],
            "last_status": dict(self._status),
        }

    def save_state(self, path: Path, *, updated_at: int | None = None) -> None:
        _write_json_atomic(path, self.state_dict(updated_at=updated_at))

    def _assemble(
        self, raw_events: Iterable[dict[str, Any]]
    ) -> tuple[list[NormalizedEpisode], int]:
        accepted_events: list[CompletionEvent] = []
        self.rejections = []
        self.exclusions = []
        raw_count = 0
        for raw in raw_events:
            raw_count += 1
            try:
                accepted_events.append(adapt_completion_event_envelope(raw))
            except OutOfScopeError as exc:
                # Production delivery with no research design set. Counted, never a failure --
                # calling this a rejection is what made a healthy-but-empty run look like a fault
                # and a faulty run look ordinary. Subclass check MUST precede EnvelopeError.
                self.exclusions.append(
                    Rejection(exc.event_id, "out_of_scope", tuple(exc.reasons))
                )
            except EnvelopeError as exc:
                self.rejections.append(
                    Rejection(exc.event_id, "envelope", tuple(exc.reasons))
                )

        grouped: dict[tuple[str, str], list[CompletionEvent]] = {}
        for event in accepted_events:
            grouped.setdefault((event.run_id, event.attempt_id), []).append(event)
        episodes: list[NormalizedEpisode] = []
        for (run_id, attempt_id), group in sorted(grouped.items()):
            episode_id = f"{run_id}:{attempt_id}"
            identities = {event.identity for event in group}
            if len(identities) != 1:
                self.rejections.append(
                    Rejection(episode_id, "episode", ("episode_identity_mismatch",))
                )
                continue
            by_phase: dict[str, CompletionEvent] = {}
            duplicate_phases: list[str] = []
            for event in sorted(group, key=lambda item: (item.occurred_at, item.event_id)):
                if event.phase in by_phase:
                    duplicate_phases.append(event.phase)
                else:
                    by_phase[event.phase] = event
            reasons = [f"missing_phase:{phase}" for phase in PHASES if phase not in by_phase]
            reasons.extend(f"duplicate_phase:{phase}" for phase in sorted(set(duplicate_phases)))
            if not reasons:
                for left, right in zip(PHASES, PHASES[1:]):
                    if by_phase[left].occurred_at > by_phase[right].occurred_at:
                        reasons.append(f"non_monotonic_phase_order:{left}->{right}")
            if reasons:
                self.rejections.append(Rejection(episode_id, "episode", tuple(reasons)))
                continue
            episodes.append(normalize_episode(by_phase))
        return episodes, raw_count

    @staticmethod
    def _dedupe_groups(episodes: list[NormalizedEpisode]) -> list[list[NormalizedEpisode]]:
        union = _UnionFind(len(episodes))
        indexes: dict[tuple[str, str], int] = {}
        exact_indexes: dict[str, int] = {}
        successor_indexes: dict[str, int] = {}
        for index, episode in enumerate(episodes):
            for key in (
                ("exact", episode.fingerprint),
                ("semantic", episode.semantic_fingerprint),
                ("output", episode.output_contract_fingerprint),
            ):
                if key in indexes:
                    union.union(index, indexes[key])
                else:
                    indexes[key] = index
            exact_indexes[episode.fingerprint] = index
            if episode.fingerprint in successor_indexes:
                union.union(index, successor_indexes[episode.fingerprint])
            for predecessor in episode.successor_of:
                if predecessor in exact_indexes:
                    union.union(index, exact_indexes[predecessor])
                if predecessor in successor_indexes:
                    union.union(index, successor_indexes[predecessor])
                successor_indexes[predecessor] = index
        groups: dict[int, list[NormalizedEpisode]] = {}
        for index, episode in enumerate(episodes):
            groups.setdefault(union.find(index), []).append(episode)
        return [groups[key] for key in sorted(groups)]

    @staticmethod
    def _terminalize_subjects(
        episodes: list[NormalizedEpisode],
    ) -> tuple[list[NormalizedEpisode], dict[str, list[NormalizedEpisode]]]:
        """Keep one terminal episode per subject and retain every retry for audit."""
        by_subject: dict[str, list[NormalizedEpisode]] = {}
        for episode in episodes:
            by_subject.setdefault(episode.identity.subject_id, []).append(episode)
        terminals: list[NormalizedEpisode] = []
        history_by_terminal: dict[str, list[NormalizedEpisode]] = {}
        for subject_id in sorted(by_subject):
            ordered = sorted(
                by_subject[subject_id],
                key=lambda item: (item.occurred_at, item.episode_id),
            )
            terminal = ordered[-1]
            terminals.append(terminal)
            history_by_terminal[terminal.episode_id] = ordered[:-1]
        return terminals, history_by_terminal

    def _candidate(
        self,
        group: list[NormalizedEpisode],
        *,
        retry_history: list[NormalizedEpisode],
        now: int,
    ) -> tuple[CapabilityIR | None, dict[str, Any]]:
        terminal_by_subject = {episode.identity.subject_id: episode for episode in group}
        terminal_ids = {episode.episode_id for episode in group}
        all_evidence = [*group, *retry_history]
        positive = [episode for episode in group if episode.positive]
        negative = [episode for episode in group if not episode.positive]
        negative_audit = [episode for episode in all_evidence if not episode.positive]
        positive_subjects = sorted({episode.identity.subject_id for episode in positive})
        negative_subjects = sorted({episode.identity.subject_id for episode in negative})
        all_evidence_subjects = set(positive_subjects) | set(negative_subjects)
        negative_ratio = len(negative_subjects) / max(1, len(all_evidence_subjects))
        progress = {
            "fingerprint": min(episode.fingerprint for episode in group),
            "positive_distinct_subjects": len(positive_subjects),
            "effective_subject_count": float(len(positive_subjects)),
            "required_positive_distinct_subjects": self.min_positive_subjects,
            "negative_distinct_subjects": len(negative_subjects),
            "negative_ratio": negative_ratio,
            "maximum_negative_ratio": self.max_negative_ratio,
            "retry_history_episode_count": len(retry_history),
            "retry_counterexample_count": sum(
                1 for episode in retry_history if not episode.positive
            ),
            "durable_positive_present": bool(positive),
            "next_action": "emit_candidate",
        }
        reasons: list[str] = []
        if len(positive_subjects) < self.min_positive_subjects:
            reasons.append(
                f"insufficient_distinct_positive_subjects:{len(positive_subjects)}/{self.min_positive_subjects}"
            )
        if negative_ratio > self.max_negative_ratio:
            reasons.append("negative_evidence_exceeds_bound")
        if not positive:
            reasons.extend(sorted({episode.evidence_reason for episode in negative}))
            reasons.append("no_durable_result")
        if reasons:
            progress["next_action"] = "wait_for_new_completion_evidence"
            progress["reasons"] = reasons
            self.rejections.append(
                Rejection(progress["fingerprint"], "evidence_gate", tuple(reasons))
            )
            return None, progress

        canonical_fingerprint = stable_hash(
            "capability-candidate-v1",
            {
                "semantic": min(item.semantic_fingerprint for item in group),
                "output": min(item.output_contract_fingerprint for item in group),
            },
        )
        capability_id = "capability:" + canonical_fingerprint.removeprefix("sha256:")[:24]
        aliases = tuple(
            sorted(
                {episode.fingerprint for episode in all_evidence}
                - {canonical_fingerprint}
            )
        )
        deduped_positives: dict[tuple[str, str], NormalizedEpisode] = {}
        for episode in sorted(positive, key=lambda item: (item.occurred_at, item.episode_id)):
            deduped_positives.setdefault(
                (episode.identity.subject_id, episode.identity.observation_id), episode
            )
        occurrences = tuple(
            SourceOccurrence(
                event_id=episode.event_ids[-1],
                event_refs=episode.event_ids,
                occurred_at=episode.occurred_at,
                subject_id=episode.identity.subject_id,
                observation_id=episode.identity.observation_id,
                family_id=episode.identity.family_id,
                canonical_target=episode.identity.canonical_target,
                repository=episode.identity.repository,
                task_type=episode.identity.task_type,
                normalized_spec_hash=episode.identity.normalized_spec_hash,
                base_sha=episode.identity.base_sha,
                profile_id=episode.identity.profile_id,
                arm_id=episode.identity.arm_id,
                attempt_id=episode.events[0].attempt_id,
                artifact_refs=episode.artifact_refs,
                verification_ref=episode.verification_ref,
                outcome_ref=episode.outcome_ref,
                durability_ref=episode.durability_ref,
            )
            for episode in deduped_positives.values()
        )
        counterexamples = tuple(
            Counterexample(
                event_id=episode.event_ids[-1],
                subject_id=episode.identity.subject_id,
                reason=episode.evidence_reason,
                verification_verdict=_norm_string(
                    (episode.events[4].payload.get("verification") or {}).get(
                        "adjudicated_verdict"
                    )
                    or (episode.events[4].payload.get("verification") or {}).get(
                        "verifier_verdict"
                    )
                    or episode.events[4].status
                ),
                outcome_verdict=_norm_string(
                    _result(episode.events[5]).get("outcome_verdict")
                    or episode.events[5].status
                ),
                durability=_norm_string(
                    (episode.events[6].payload.get("durability") or {}).get("status")
                    or episode.events[6].status
                ),
                evidence_refs=tuple(
                    value
                    for value in (
                        episode.verification_ref,
                        episode.outcome_ref,
                        episode.durability_ref,
                    )
                    if value
                ),
                event_refs=episode.event_ids,
                observation_id=episode.identity.observation_id,
                attempt_id=episode.identity.attempt_id,
                terminal=episode.episode_id in terminal_ids,
                audit_class=(
                    "terminal_counterexample"
                    if episode.episode_id in terminal_ids
                    else "superseded_retry_counterexample"
                ),
            )
            for episode in negative_audit
        )
        observations_by_subject: dict[str, set[str]] = {}
        for occurrence in occurrences:
            observations_by_subject.setdefault(occurrence.subject_id, set()).add(
                occurrence.observation_id
            )
        observation_weights = {
            occurrence.observation_id: 1.0
            / len(observations_by_subject[occurrence.subject_id])
            for occurrence in occurrences
        }
        representative = min(
            positive,
            key=lambda item: (
                item.output_contract_fingerprint,
                item.semantic_fingerprint,
                item.fingerprint,
            ),
        )
        last_evidence_at = max(item.occurred_at for item in all_evidence)
        candidate = CapabilityIR(
            capability_id=capability_id,
            fingerprint=canonical_fingerprint,
            semantic_fingerprint=representative.semantic_fingerprint,
            output_contract_fingerprint=representative.output_contract_fingerprint,
            kind_proposal=representative.kind_proposal,  # type: ignore[arg-type]
            owner_proposal=representative.owner_proposal,  # type: ignore[arg-type]
            source_occurrences=occurrences,
            counterexamples=counterexamples,
            independent_subjects=tuple(positive_subjects),
            independent_repositories=tuple(
                sorted({item.identity.repository for item in positive})
            ),
            selector=representative.selector,
            graph=representative.graph,
            artifact_refs=tuple(
                sorted({ref for item in positive for ref in item.artifact_refs})
            ),
            gates={
                "minimum_positive_subjects": self.min_positive_subjects,
                "maximum_negative_ratio": self.max_negative_ratio,
                "named_verification_required": True,
                "accepted_artifact_required": True,
                "durable_result_required": True,
            },
            telemetry={
                "accepted_episode_count": len(positive),
                "evidence_count": len(occurrences),
                "raw_evidence_count": len(all_evidence),
                "terminal_episode_count": len(terminal_by_subject),
                "retry_history_episode_count": len(retry_history),
                "retry_counterexample_count": sum(
                    1 for episode in retry_history if not episode.positive
                ),
                "distinct_subject_count": len(positive_subjects),
                "distinct_repository_count": len(
                    {item.identity.repository for item in positive}
                ),
                "counterexample_count": len(counterexamples),
                "negative_ratio": negative_ratio,
                "observation_weights": observation_weights,
                "effective_subject_count": sum(observation_weights.values()),
                "last_evidence_at": last_evidence_at,
            },
            lifecycle=Lifecycle(expires_at=last_evidence_at + self.ttl_seconds),
            aliases=aliases,
            predecessor=(
                min({value for item in group for value in item.successor_of})
                if any(item.successor_of for item in group)
                else None
            ),
        )
        candidate.validate()
        progress["next_action"] = "observe_until_ttl_or_compiler_intake"
        progress["capability_id"] = capability_id
        return candidate, progress

    def mine(
        self,
        raw_events: Iterable[dict[str, Any]],
        *,
        now: int | None = None,
        preserve_existing: bool = False,
    ) -> MiningResult:
        current = int(time.time()) if now is None else int(now)
        episodes, raw_count = self._assemble(raw_events)
        terminal_episodes, retry_history_by_terminal = self._terminalize_subjects(
            episodes
        )
        existing_candidates = list(self.candidates) if preserve_existing else []
        existing_tombstones = list(self.tombstones) if preserve_existing else []
        self.candidates = []
        self.tombstones = []
        progress: list[dict[str, Any]] = []
        for group in self._dedupe_groups(terminal_episodes):
            retry_history = [
                retry
                for terminal in group
                for retry in retry_history_by_terminal.get(terminal.episode_id, ())
            ]
            candidate, group_progress = self._candidate(
                group,
                retry_history=retry_history,
                now=current,
            )
            progress.append(group_progress)
            if candidate:
                self.candidates.append(candidate)
                for alias in candidate.aliases:
                    self.tombstones.append(
                        CandidateTombstone(
                            fingerprint=alias,
                            capability_id=None,
                            reason="deduplicated_alias",
                            retired_at=current,
                            successor=candidate.capability_id,
                        )
                    )
        if preserve_existing:
            candidates_by_fingerprint = {
                item.fingerprint: item for item in existing_candidates
            }
            for candidate in self.candidates:
                previous = candidates_by_fingerprint.get(candidate.fingerprint)
                previous_evidence_at = int(
                    (previous.telemetry if previous else {}).get("last_evidence_at") or 0
                )
                candidate_evidence_at = int(
                    candidate.telemetry.get("last_evidence_at") or 0
                )
                if (
                    previous is None
                    or previous.lifecycle.state == "clustered"
                    or candidate_evidence_at > previous_evidence_at
                ):
                    candidates_by_fingerprint[candidate.fingerprint] = candidate
            self.candidates = list(candidates_by_fingerprint.values())
            tombstones_by_identity = {
                (
                    item.fingerprint,
                    item.reason,
                    item.capability_id,
                    item.successor,
                ): item
                for item in [*existing_tombstones, *self.tombstones]
            }
            self.tombstones = list(tombstones_by_identity.values())
        self.candidates.sort(key=lambda item: item.fingerprint)
        self.tombstones.sort(
            key=lambda item: (
                item.fingerprint,
                item.reason,
                item.capability_id or "",
                item.successor or "",
            )
        )
        next_actions = sorted({item["next_action"] for item in progress})
        if not next_actions and any(
            item.lifecycle.state == "clustered" for item in self.candidates
        ):
            next_actions = ["observe_until_ttl_or_compiler_intake"]
        self._status = {
            "schema": STATUS_SCHEMA,
            "version": STATUS_VERSION,
            "input_contract": {
                "schema": "orchestrator.completion-event-envelope",
                "version": 1,
                "transport": "JSON array or JSONL; '-' reads JSONL from stdin",
                "mode": "complete snapshot; state preserves lifecycle on empty cadence",
            },
            "state_contract": {
                "schema": STATE_SCHEMA,
                "version": STATE_VERSION,
                "contains": ["candidates", "tombstones", "last_status"],
            },
            "state_loaded": self.state_loaded,
            "accepted_event_count": raw_count
            - sum(1 for rejection in self.rejections if rejection.phase == "envelope")
            - len(self.exclusions),
            "rejected_event_count": sum(
                1 for rejection in self.rejections if rejection.phase == "envelope"
            ),
            # Excluded != rejected. These events legitimately have no research subject, so a large
            # exclusion count is the NORMAL shape of a production stream, not a defect signal.
            "excluded_event_count": len(self.exclusions),
            "excluded_event_reasons": _reason_counts(self.exclusions),
            "excluded_producers": _producer_counts(self.exclusions),
            "raw_event_count": raw_count,
            "complete_episode_count": len(episodes),
            "terminal_episode_count": len(terminal_episodes),
            "collapsed_retry_episode_count": len(episodes) - len(terminal_episodes),
            "distinct_eligible_subjects": len(
                {
                    episode.identity.subject_id
                    for episode in terminal_episodes
                    if episode.positive
                }
            ),
            "emitted_candidate_count": sum(
                1 for item in self.candidates if item.lifecycle.state == "clustered"
            ),
            "expired_candidate_count": sum(
                1 for item in self.candidates if item.lifecycle.state == "retired"
            ),
            "rejection_count": len(self.rejections),
            # The single line a cadence step can act on. `orchestrate.sh` inspected only the exit
            # code, so a run that accepted 0 of 1784 called _mark_success and looked healthy --
            # which is how this stayed invisible for 43 days while ALERTing into an unread log.
            # Blocking quantity and drainable quantity in the same place (CLAUDE.md runtime rule).
            "mining_health": _mining_health(
                raw_count,
                raw_count
                - sum(1 for r in self.rejections if r.phase == "envelope")
                - len(self.exclusions),
                sum(1 for r in self.rejections if r.phase == "envelope"),
                len(self.exclusions),
                len(episodes),
                len(self.candidates),
                _reason_counts(
                    rejection for rejection in self.rejections
                    if rejection.phase == "envelope"
                ),
            ),
            "rejected_event_reasons": _reason_counts(
                rejection for rejection in self.rejections
                if rejection.phase == "envelope"
            ),
            "threshold_progress": progress,
            "next_actions": next_actions,
            "human_review_queue_count": 0,
        }
        return self.result()

    def sweep(self, *, now: int | None = None) -> MiningResult:
        """Expire stale candidates automatically and retain their fingerprints."""
        current = int(time.time()) if now is None else int(now)
        updated: list[CapabilityIR] = []
        expired = 0
        existing_tombstones = {item.fingerprint for item in self.tombstones}
        for candidate in self.candidates:
            if (
                candidate.lifecycle.state == "clustered"
                and current >= candidate.lifecycle.expires_at
            ):
                expired += 1
                lifecycle = replace(
                    candidate.lifecycle,
                    state="retired",
                    next_automatic_action="none",
                    expiry_reason="no_new_evidence_before_candidate_ttl",
                )
                candidate = replace(candidate, lifecycle=lifecycle)
                if candidate.fingerprint not in existing_tombstones:
                    self.tombstones.append(
                        CandidateTombstone(
                            fingerprint=candidate.fingerprint,
                            capability_id=candidate.capability_id,
                            reason="no_new_evidence_before_candidate_ttl",
                            retired_at=current,
                            aliases=candidate.aliases,
                        )
                    )
                    existing_tombstones.add(candidate.fingerprint)
            updated.append(candidate)
        self.candidates = updated
        self._status["expired_candidate_count"] = sum(
            1 for item in self.candidates if item.lifecycle.state == "retired"
        )
        self._status["emitted_candidate_count"] = sum(
            1 for item in self.candidates if item.lifecycle.state == "clustered"
        )
        if expired:
            self._status["next_actions"] = sorted(
                set(self._status.get("next_actions") or []) | {"retain_tombstone"}
            )
        return self.result()

    def result(self) -> MiningResult:
        return MiningResult(
            candidates=tuple(self.candidates),
            rejections=tuple(self.rejections),
            tombstones=tuple(self.tombstones),
            status=dict(self._status),
        )


def _read_envelopes(path: Path) -> list[dict[str, Any]]:
    text = sys.stdin.read() if str(path) == "-" else path.read_text(encoding="utf-8")
    if not text.strip():
        return []
    if path.suffix.lower() == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    if str(path) == "-":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    payload = json.loads(text)
    if not isinstance(payload, list):
        raise ValueError("completion event input must be a JSON list or JSONL records")
    return payload


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "inventory"):
        command = sub.add_parser(name)
        command.add_argument("--events", type=Path, required=True)
        command.add_argument(
            "--state",
            type=Path,
            help="load and atomically persist the versioned lifecycle state",
        )
        command.add_argument("--now", type=int)
        command.add_argument("--ttl-days", type=int, default=DEFAULT_CANDIDATE_TTL_DAYS)
        command.add_argument(
            "--write",
            type=Path,
            help="atomically write the cadence-ready JSON status or inventory artifact",
        )
    cadence = sub.add_parser(
        "run",
        help="cadence-ready JSONL runner with durable state and both report artifacts",
    )
    cadence.add_argument("--events", type=Path, default=Path("-"))
    cadence.add_argument("--state", type=Path, required=True)
    cadence.add_argument("--status-out", type=Path, required=True)
    cadence.add_argument("--inventory-out", type=Path, required=True)
    cadence.add_argument("--now", type=int)
    cadence.add_argument("--ttl-days", type=int, default=DEFAULT_CANDIDATE_TTL_DAYS)
    args = parser.parse_args(argv)
    miner = PatternMiner(candidate_ttl_days=args.ttl_days)
    if args.state:
        miner.load_state(args.state)
    result = miner.mine(
        _read_envelopes(args.events),
        now=args.now,
        preserve_existing=miner.state_loaded,
    )
    result = miner.sweep(now=args.now)
    if args.state:
        miner.save_state(args.state, updated_at=args.now)
    if args.command == "run":
        _write_json_atomic(args.status_out, result.to_dict(include_candidates=False))
        _write_json_atomic(args.inventory_out, result.to_dict(include_candidates=True))
        payload = result.to_dict(include_candidates=False)
    else:
        payload = result.to_dict(include_candidates=args.command == "inventory")
        if args.write:
            _write_json_atomic(args.write, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

