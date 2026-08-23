#!/usr/bin/env python3
"""Typed intermediate representation for mined Orchestrator capabilities.

This module is deliberately generation-free.  A :class:`CapabilityIR` is an
evidence-backed *candidate* that may later be consumed by a separate compiler;
it is never executable and never implies activation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from typing import Any, Literal

IR_SCHEMA = "orchestrator.capability-ir"
IR_VERSION = 2

CapabilityKind = Literal[
    "acceptance_gate",
    "playbook",
    "role",
    "skill",
    "workflow",
    "unknown",
]
CapabilityOwner = Literal["orchestrator", "repo", "shared", "workflows", "unknown"]


def canonical_json(value: Any) -> str:
    """Return the stable serialization used for every identifier."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def stable_hash(namespace: str, value: Any) -> str:
    payload = f"{namespace}\0{canonical_json(value)}".encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class SourceOccurrence:
    event_id: str
    event_refs: tuple[str, ...]
    occurred_at: int
    subject_id: str
    observation_id: str
    family_id: str | None
    canonical_target: str
    repository: str
    task_type: str
    normalized_spec_hash: str
    base_sha: str
    profile_id: str
    arm_id: str
    attempt_id: str
    artifact_refs: tuple[str, ...] = ()
    verification_ref: str | None = None
    outcome_ref: str | None = None
    durability_ref: str | None = None


@dataclass(frozen=True)
class Counterexample:
    event_id: str
    subject_id: str
    reason: str
    verification_verdict: str
    outcome_verdict: str
    durability: str
    evidence_refs: tuple[str, ...] = ()
    event_refs: tuple[str, ...] = ()
    observation_id: str | None = None
    attempt_id: str | None = None
    terminal: bool = True
    audit_class: str = "terminal_counterexample"


@dataclass(frozen=True)
class Lifecycle:
    state: str = "clustered"
    next_automatic_action: str = "expire_to_tombstone"
    expires_at: int = 0
    expiry_reason: str | None = None
    rollback: dict[str, Any] = field(
        default_factory=lambda: {
            "action": "retire_candidate",
            "reason": "evidence regressed or candidate expired",
        }
    )


@dataclass(frozen=True)
class CapabilityIR:
    capability_id: str
    fingerprint: str
    semantic_fingerprint: str
    output_contract_fingerprint: str
    kind_proposal: CapabilityKind
    owner_proposal: CapabilityOwner
    source_occurrences: tuple[SourceOccurrence, ...]
    counterexamples: tuple[Counterexample, ...]
    independent_subjects: tuple[str, ...]
    independent_repositories: tuple[str, ...]
    selector: dict[str, Any]
    graph: dict[str, Any]
    artifact_refs: tuple[str, ...]
    gates: dict[str, Any]
    telemetry: dict[str, Any]
    lifecycle: Lifecycle
    aliases: tuple[str, ...] = ()
    predecessor: str | None = None
    successor: str | None = None
    schema: str = IR_SCHEMA
    version: int = IR_VERSION

    def validate(self) -> None:
        if self.schema != IR_SCHEMA or self.version != IR_VERSION:
            raise ValueError("unsupported capability IR schema")
        if not self.capability_id.startswith("capability:"):
            raise ValueError("invalid capability_id")
        if not self.fingerprint.startswith("sha256:"):
            raise ValueError("invalid capability fingerprint")
        if self.lifecycle.state not in {"clustered", "retired", "superseded"}:
            raise ValueError("miner IR may not claim generated or active lifecycle states")
        if self.kind_proposal not in {
            "acceptance_gate",
            "playbook",
            "role",
            "skill",
            "workflow",
            "unknown",
        }:
            raise ValueError("invalid capability kind proposal")
        if self.owner_proposal not in {
            "orchestrator",
            "repo",
            "shared",
            "workflows",
            "unknown",
        }:
            raise ValueError("invalid capability owner proposal")
        if self.telemetry.get("distinct_subject_count") != len(self.independent_subjects):
            raise ValueError("distinct subject telemetry does not match source evidence")
        if not self.source_occurrences:
            raise ValueError("capability IR requires source occurrences")
        if tuple(self.graph.get("phase_order") or ()) != (
            "trigger",
            "decision",
            "execution",
            "artifact",
            "verification",
            "outcome",
            "durability",
        ):
            raise ValueError("capability IR requires the canonical completion graph")
        for occurrence in self.source_occurrences:
            if len(occurrence.event_refs) != 7 or len(set(occurrence.event_refs)) != 7:
                raise ValueError("source occurrence requires all seven event refs")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CapabilityIR":
        """Restore one persisted v2 candidate without accepting loose shapes."""
        if raw.get("schema") != IR_SCHEMA or raw.get("version") != IR_VERSION:
            raise ValueError("unsupported persisted capability IR schema")
        payload = dict(raw)
        payload["source_occurrences"] = tuple(
            SourceOccurrence(
                **{
                    **item,
                    "event_refs": tuple(item.get("event_refs") or ()),
                    "artifact_refs": tuple(item.get("artifact_refs") or ()),
                }
            )
            for item in raw.get("source_occurrences") or ()
        )
        payload["counterexamples"] = tuple(
            Counterexample(
                **{
                    **item,
                    "evidence_refs": tuple(item.get("evidence_refs") or ()),
                    "event_refs": tuple(item.get("event_refs") or ()),
                }
            )
            for item in raw.get("counterexamples") or ()
        )
        payload["independent_subjects"] = tuple(raw.get("independent_subjects") or ())
        payload["independent_repositories"] = tuple(raw.get("independent_repositories") or ())
        payload["artifact_refs"] = tuple(raw.get("artifact_refs") or ())
        payload["aliases"] = tuple(raw.get("aliases") or ())
        payload["lifecycle"] = Lifecycle(**raw["lifecycle"])
        candidate = cls(**payload)
        candidate.validate()
        return candidate


@dataclass(frozen=True)
class CandidateTombstone:
    fingerprint: str
    capability_id: str | None
    reason: str
    retired_at: int
    successor: str | None = None
    aliases: tuple[str, ...] = ()
    schema: str = "orchestrator.capability-tombstone"
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CandidateTombstone":
        payload = dict(raw)
        payload["aliases"] = tuple(raw.get("aliases") or ())
        tombstone = cls(**payload)
        if tombstone.schema != "orchestrator.capability-tombstone" or tombstone.version != 1:
            raise ValueError("unsupported persisted capability tombstone schema")
        return tombstone
