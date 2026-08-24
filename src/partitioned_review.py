#!/usr/bin/env python3
"""Bounded, schema-validated review/reconciliation over large corpora.

The transport remains :func:`dispatcher.offload`.  This module only adds deterministic
partitioning, result validation, provenance envelopes, and fail-closed synthesis so a large
corpus is never mistaken for one successful (or timed-out) prompt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import time
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_MAX_ITEMS = 10
DEFAULT_MAX_PROMPT_CHARS = 24_000
DEFAULT_PARTITION_TIMEOUT = 300

CATEGORY_KEYS = (
    "removed_product_surfaces",
    "test_only_runtime_seams",
    "intentional_adapters",
    "historical_negative_assertions",
    "confirmed_defects",
    "unresolved_design_dispositions",
)
DISPOSITIONS = {
    "satisfied",
    "remaining",
    "partial",
    "intentional",
    "historical_only",
    "unresolved",
    "not_applicable",
}
CONFIDENCES = {"low", "medium", "high"}
EVIDENCE_TYPES = {
    "current_code",
    "test",
    "runtime",
    "history",
    "design_record",
    "absence_gate",
    "name_scan",
}
ADJUDICATION_DECISIONS = {
    "uphold_finding",
    "reject_false_positive",
    "needs_more_evidence",
    "owner_disposition_required",
}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _without_digest(value: dict[str, Any], field: str) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != field}


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _string_list(value: Any, path: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list):
        return [f"{path} must be a list"]
    errors = [f"{path} must be non-empty"] if nonempty and not value else []
    for index, item in enumerate(value):
        if not _is_text(item):
            errors.append(f"{path}[{index}] must be a non-empty string")
    return errors


def _atomic_json(path: str | Path, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _parse_json_object(content: str) -> dict[str, Any]:
    stripped = (content or "").strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError as direct_error:
        messages: list[str] = []
        for line in stripped.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                raise direct_error
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise direct_error
            message = _agent_message_text(event)
            if message is not None:
                messages.append(message)
        if not messages:
            raise direct_error
        try:
            return _parse_json_object(messages[-1])
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError("final agent_message did not contain one strict JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("result must decode to one JSON object")
    message = _agent_message_text(value)
    if message is not None:
        try:
            return _parse_json_object(message)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError("agent_message did not contain one strict JSON object") from exc
    return value


def _agent_message_text(event: dict[str, Any]) -> str | None:
    item = event.get("item")
    if (
        event.get("type") == "item.completed"
        and isinstance(item, dict)
        and item.get("type") == "agent_message"
    ):
        message = item.get("text")
    elif event.get("type") == "agent_message":
        message = event.get("text")
    else:
        return None
    if not isinstance(message, str) or not message.strip():
        raise ValueError("agent_message text must be a non-empty string")
    return message


def _source_ref_errors(value: Any, path: str) -> list[str]:
    if not isinstance(value, list) or not value:
        return [f"{path} must be a non-empty list"]
    errors: list[str] = []
    for index, ref in enumerate(value):
        item_path = f"{path}[{index}]"
        if not isinstance(ref, dict):
            errors.append(f"{item_path} must be an object")
            continue
        unknown = sorted(set(ref) - {"kind", "ref", "revision"})
        if unknown:
            errors.append(f"{item_path} has unknown keys: {unknown}")
        if not _is_text(ref.get("kind")):
            errors.append(f"{item_path}.kind must be a non-empty string")
        if not _is_text(ref.get("ref")):
            errors.append(f"{item_path}.ref must be a non-empty string")
        if ref.get("revision") is not None and not _is_text(ref.get("revision")):
            errors.append(f"{item_path}.revision must be a non-empty string or null")
    return errors


def validate_corpus(corpus: Any) -> list[str]:
    if not isinstance(corpus, dict):
        return ["corpus must be a JSON object"]
    errors: list[str] = []
    unknown = sorted(
        set(corpus) - {"schema_version", "review_id", "objective", "shared_context", "items"}
    )
    if unknown:
        errors.append(f"corpus has unknown keys: {unknown}")
    if corpus.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"corpus.schema_version must equal {SCHEMA_VERSION}")
    for key in ("review_id", "objective"):
        if not _is_text(corpus.get(key)):
            errors.append(f"corpus.{key} must be a non-empty string")
    errors.extend(_string_list(corpus.get("shared_context", []), "corpus.shared_context"))
    items = corpus.get("items")
    if not isinstance(items, list) or not items:
        return errors + ["corpus.items must be a non-empty list"]
    seen: set[str] = set()
    for index, item in enumerate(items):
        path = f"corpus.items[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{path} must be an object")
            continue
        unknown = sorted(
            set(item)
            - {"item_id", "assertion_key", "group_key", "assertion", "context", "source_refs"}
        )
        if unknown:
            errors.append(f"{path} has unknown keys: {unknown}")
        for key in ("item_id", "group_key", "assertion"):
            if not _is_text(item.get(key)):
                errors.append(f"{path}.{key} must be a non-empty string")
        item_id = str(item.get("item_id") or "").strip()
        if item_id in seen:
            errors.append(f"{path}.item_id duplicates {item_id!r}")
        seen.add(item_id)
        if item.get("assertion_key") is not None and not _is_text(item.get("assertion_key")):
            errors.append(f"{path}.assertion_key must be a non-empty string or null")
        if item.get("context") is not None and not _is_text(item.get("context")):
            errors.append(f"{path}.context must be a non-empty string or null")
        errors.extend(_source_ref_errors(item.get("source_refs"), f"{path}.source_refs"))
    return errors


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (slug or "group")[:48]


def _normalized_item(item: dict[str, Any]) -> dict[str, Any]:
    out = dict(item)
    out["item_id"] = item["item_id"].strip()
    out["assertion_key"] = (item.get("assertion_key") or item["item_id"]).strip()
    out["group_key"] = item["group_key"].strip()
    out["assertion"] = item["assertion"].strip()
    if out.get("context") is not None:
        out["context"] = out["context"].strip()
    return out


def _partition_with_digest(partition: dict[str, Any]) -> dict[str, Any]:
    out = dict(partition)
    out["partition_digest"] = _digest(out)
    return out


def _plan_with_digest(plan: dict[str, Any]) -> dict[str, Any]:
    out = dict(plan)
    out["plan_sha256"] = _digest(out)
    return out


def _empty_categories() -> dict[str, list[Any]]:
    return {key: [] for key in CATEGORY_KEYS}


def build_partition_prompt(plan: dict[str, Any], partition: dict[str, Any]) -> str:
    result_shape = {
        "schema_version": SCHEMA_VERSION,
        "review_id": plan["review_id"],
        "partition_id": partition["partition_id"],
        "partition_digest": partition["partition_digest"],
        "categories": _empty_categories(),
        "summary": "partition-level synthesis",
    }
    finding_shape = {
        "item_id": "exact corpus item id",
        "assertion_key": "exact corpus assertion key",
        "disposition": "satisfied|remaining|partial|intentional|historical_only|unresolved|not_applicable",
        "summary": "current evidence-led conclusion",
        "evidence": [
            {
                "type": "current_code|test|runtime|history|design_record|absence_gate|name_scan",
                "ref": "stable file:line, test, PR, commit, or artifact reference",
                "observation": "what the referenced evidence establishes",
            }
        ],
        "confidence": "low|medium|high",
        "recommended_action": "bounded next action or null",
    }
    payload = {
        "review_id": plan["review_id"],
        "plan_sha256": plan["plan_sha256"],
        "objective": plan["objective"],
        "shared_context": plan.get("shared_context", []),
        "partition": partition,
    }
    return (
        "Review exactly one bounded reconciliation partition. Inspect the cited sources in the current "
        "workspace when available; do not infer a defect from matching names alone. A raw name/token scan "
        "is discovery evidence only and may never be the sole evidence for a finding. Distinguish removed "
        "product surfaces from test-only runtime seams, intentional adapters, historical/negative "
        "assertions, confirmed defects, and unresolved design dispositions. Place every item in exactly "
        "one category, copy item_id/assertion_key exactly, and return STRICT JSON only. Category and "
        "disposition must agree: every intentional_adapters item uses disposition intentional, and every "
        "unresolved_design_dispositions item uses disposition unresolved. Choose the category from the "
        "current implementation; record a stale or incomplete ledger in evidence/recommended_action "
        "without changing an otherwise intentional implementation disposition to partial. If the partition "
        "cannot be completed, return OFFLOAD_INCOMPLETE instead of a partial JSON verdict.\n\n"
        "Partition payload:\n"
        f"{json.dumps(payload, indent=2, sort_keys=True)}\n\n"
        "Required result shape (all six category keys are mandatory, even when empty):\n"
        f"{json.dumps(result_shape, indent=2, sort_keys=True)}\n\n"
        "Each category list entry must match this finding shape (do not copy it into empty categories):\n"
        f"{json.dumps(finding_shape, indent=2, sort_keys=True)}"
    )


def partition_corpus(
    corpus: dict[str, Any],
    *,
    max_items: int = DEFAULT_MAX_ITEMS,
    max_prompt_chars: int = DEFAULT_MAX_PROMPT_CHARS,
) -> dict[str, Any]:
    errors = validate_corpus(corpus)
    if errors:
        raise ValueError("invalid corpus: " + "; ".join(errors))
    if max_items < 1:
        raise ValueError("max_items must be >= 1")
    if max_prompt_chars < 2_000:
        raise ValueError("max_prompt_chars must be >= 2000")

    groups: dict[str, list[dict[str, Any]]] = {}
    for raw in corpus["items"]:
        item = _normalized_item(raw)
        groups.setdefault(item["group_key"], []).append(item)

    partitions: list[dict[str, Any]] = []
    ordinal = 0
    skeleton = {
        "schema_version": SCHEMA_VERSION,
        "review_id": corpus["review_id"].strip(),
        "objective": corpus["objective"].strip(),
        "shared_context": corpus.get("shared_context", []),
        "limits": {"max_items": max_items, "max_prompt_chars": max_prompt_chars},
        "partitions": [],
        "plan_sha256": "0" * 64,
    }

    def candidate(group_key: str, items: list[dict[str, Any]], number: int) -> dict[str, Any]:
        return _partition_with_digest(
            {
                "partition_id": f"p{number:03d}-{_slug(group_key)}",
                "group_key": group_key,
                "items": items,
            }
        )

    for group_key, items in groups.items():
        current: list[dict[str, Any]] = []
        for item in items:
            next_items = [*current, item]
            proposed = candidate(group_key, next_items, ordinal + 1)
            too_large = len(build_partition_prompt(skeleton, proposed)) > max_prompt_chars
            if current and (len(next_items) > max_items or too_large):
                ordinal += 1
                partitions.append(candidate(group_key, current, ordinal))
                current = [item]
                next_items = current
                proposed = candidate(group_key, current, ordinal + 1)
                too_large = len(build_partition_prompt(skeleton, proposed)) > max_prompt_chars
            if len(next_items) > max_items or too_large:
                raise ValueError(
                    f"corpus item {item['item_id']!r} cannot fit max_prompt_chars={max_prompt_chars}"
                )
            current = next_items
        if current:
            ordinal += 1
            partitions.append(candidate(group_key, current, ordinal))

    plan = _plan_with_digest(
        {
            "schema_version": SCHEMA_VERSION,
            "review_id": corpus["review_id"].strip(),
            "objective": corpus["objective"].strip(),
            "shared_context": corpus.get("shared_context", []),
            "limits": {"max_items": max_items, "max_prompt_chars": max_prompt_chars},
            "partitions": partitions,
        }
    )
    plan_errors = validate_plan(plan)
    if plan_errors:
        raise AssertionError("generated invalid plan: " + "; ".join(plan_errors))
    for partition in partitions:
        if len(build_partition_prompt(plan, partition)) > max_prompt_chars:
            raise ValueError(f"partition {partition['partition_id']} exceeds max_prompt_chars")
    return plan


def validate_plan(plan: Any) -> list[str]:
    if not isinstance(plan, dict):
        return ["plan must be a JSON object"]
    errors: list[str] = []
    required = {
        "schema_version",
        "review_id",
        "objective",
        "shared_context",
        "limits",
        "partitions",
        "plan_sha256",
    }
    unknown = sorted(set(plan) - required)
    missing = sorted(required - set(plan))
    if missing:
        errors.append(f"plan missing keys: {missing}")
    if unknown:
        errors.append(f"plan has unknown keys: {unknown}")
    if errors:
        return errors
    if plan["schema_version"] != SCHEMA_VERSION:
        errors.append(f"plan.schema_version must equal {SCHEMA_VERSION}")
    for key in ("review_id", "objective"):
        if not _is_text(plan.get(key)):
            errors.append(f"plan.{key} must be a non-empty string")
    errors.extend(_string_list(plan.get("shared_context"), "plan.shared_context"))
    limits = plan.get("limits")
    if not isinstance(limits, dict) or set(limits) != {"max_items", "max_prompt_chars"}:
        errors.append("plan.limits must contain exactly max_items and max_prompt_chars")
        limits = {}
    max_items = limits.get("max_items")
    max_chars = limits.get("max_prompt_chars")
    if not isinstance(max_items, int) or max_items < 1:
        errors.append("plan.limits.max_items must be an integer >= 1")
    if not isinstance(max_chars, int) or max_chars < 2_000:
        errors.append("plan.limits.max_prompt_chars must be an integer >= 2000")
    partitions = plan.get("partitions")
    if not isinstance(partitions, list) or not partitions:
        errors.append("plan.partitions must be a non-empty list")
        partitions = []
    partition_ids: set[str] = set()
    item_ids: set[str] = set()
    for p_index, partition in enumerate(partitions):
        path = f"plan.partitions[{p_index}]"
        if not isinstance(partition, dict) or set(partition) != {
            "partition_id",
            "group_key",
            "items",
            "partition_digest",
        }:
            errors.append(
                f"{path} must contain exactly partition_id, group_key, items, partition_digest"
            )
            continue
        for key in ("partition_id", "group_key", "partition_digest"):
            if not _is_text(partition.get(key)):
                errors.append(f"{path}.{key} must be a non-empty string")
        partition_id = str(partition.get("partition_id") or "")
        if partition_id in partition_ids:
            errors.append(f"{path}.partition_id duplicates {partition_id!r}")
        partition_ids.add(partition_id)
        items = partition.get("items")
        if not isinstance(items, list) or not items:
            errors.append(f"{path}.items must be a non-empty list")
            continue
        if isinstance(max_items, int) and len(items) > max_items:
            errors.append(f"{path}.items exceeds max_items={max_items}")
        for i_index, item in enumerate(items):
            item_path = f"{path}.items[{i_index}]"
            if not isinstance(item, dict):
                errors.append(f"{item_path} must be an object")
                continue
            for key in ("item_id", "assertion_key", "group_key", "assertion"):
                if not _is_text(item.get(key)):
                    errors.append(f"{item_path}.{key} must be a non-empty string")
            item_id = str(item.get("item_id") or "")
            if item_id in item_ids:
                errors.append(f"{item_path}.item_id duplicates {item_id!r}")
            item_ids.add(item_id)
            if item.get("group_key") != partition.get("group_key"):
                errors.append(f"{item_path}.group_key must match its partition")
            errors.extend(_source_ref_errors(item.get("source_refs"), f"{item_path}.source_refs"))
        expected_digest = _digest(_without_digest(partition, "partition_digest"))
        if partition.get("partition_digest") != expected_digest:
            errors.append(f"{path}.partition_digest does not match content")
        if isinstance(max_chars, int) and max_chars >= 2_000:
            try:
                if len(build_partition_prompt(plan, partition)) > max_chars:
                    errors.append(f"{path} exceeds max_prompt_chars={max_chars}")
            except Exception as exc:
                errors.append(f"{path} prompt cannot be rendered: {exc}")
    expected_plan_digest = _digest(_without_digest(plan, "plan_sha256"))
    if plan.get("plan_sha256") != expected_plan_digest:
        errors.append("plan.plan_sha256 does not match content")
    return errors


def _finding_errors(
    finding: Any, path: str, expected_items: dict[str, dict[str, Any]]
) -> list[str]:
    if not isinstance(finding, dict):
        return [f"{path} must be an object"]
    required = {
        "item_id",
        "assertion_key",
        "disposition",
        "summary",
        "evidence",
        "confidence",
        "recommended_action",
    }
    errors: list[str] = []
    missing = sorted(required - set(finding))
    unknown = sorted(set(finding) - required)
    if missing:
        errors.append(f"{path} missing keys: {missing}")
    if unknown:
        errors.append(f"{path} has unknown keys: {unknown}")
    if missing:
        return errors
    item_id = finding.get("item_id")
    if item_id not in expected_items:
        errors.append(f"{path}.item_id is not in the partition: {item_id!r}")
    elif finding.get("assertion_key") != expected_items[item_id]["assertion_key"]:
        errors.append(f"{path}.assertion_key must match the corpus item")
    if finding.get("disposition") not in DISPOSITIONS:
        errors.append(f"{path}.disposition must be one of {sorted(DISPOSITIONS)}")
    if not _is_text(finding.get("summary")):
        errors.append(f"{path}.summary must be a non-empty string")
    if finding.get("confidence") not in CONFIDENCES:
        errors.append(f"{path}.confidence must be one of {sorted(CONFIDENCES)}")
    if finding.get("recommended_action") is not None and not _is_text(
        finding.get("recommended_action")
    ):
        errors.append(f"{path}.recommended_action must be a non-empty string or null")
    evidence = finding.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        errors.append(f"{path}.evidence must be a non-empty list")
    else:
        evidence_types: set[str] = set()
        for index, row in enumerate(evidence):
            row_path = f"{path}.evidence[{index}]"
            if not isinstance(row, dict) or set(row) != {"type", "ref", "observation"}:
                errors.append(f"{row_path} must contain exactly type, ref, observation")
                continue
            if row.get("type") not in EVIDENCE_TYPES:
                errors.append(f"{row_path}.type must be one of {sorted(EVIDENCE_TYPES)}")
            else:
                evidence_types.add(row["type"])
            if not _is_text(row.get("ref")) or not _is_text(row.get("observation")):
                errors.append(f"{row_path}.ref and observation must be non-empty strings")
        if evidence_types and evidence_types <= {"name_scan"}:
            errors.append(f"{path}.evidence cannot rely only on raw name scans")
    return errors


def validate_partition_result(
    result: Any, plan: dict[str, Any], partition: dict[str, Any]
) -> list[str]:
    if not isinstance(result, dict):
        return ["partition result must be a JSON object"]
    required = {
        "schema_version",
        "review_id",
        "partition_id",
        "partition_digest",
        "categories",
        "summary",
    }
    errors: list[str] = []
    missing = sorted(required - set(result))
    unknown = sorted(set(result) - required)
    if missing:
        errors.append(f"result missing keys: {missing}")
    if unknown:
        errors.append(f"result has unknown keys: {unknown}")
    if missing:
        return errors
    if result.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"result.schema_version must equal {SCHEMA_VERSION}")
    for key, expected in (
        ("review_id", plan["review_id"]),
        ("partition_id", partition["partition_id"]),
        ("partition_digest", partition["partition_digest"]),
    ):
        if result.get(key) != expected:
            errors.append(f"result.{key} must equal {expected!r}")
    if not _is_text(result.get("summary")):
        errors.append("result.summary must be a non-empty string")
    categories = result.get("categories")
    if not isinstance(categories, dict) or set(categories) != set(CATEGORY_KEYS):
        errors.append(f"result.categories must contain exactly {list(CATEGORY_KEYS)}")
        return errors
    expected_items = {item["item_id"]: item for item in partition["items"]}
    seen: list[str] = []
    for category in CATEGORY_KEYS:
        rows = categories[category]
        if not isinstance(rows, list):
            errors.append(f"result.categories.{category} must be a list")
            continue
        for index, finding in enumerate(rows):
            path = f"result.categories.{category}[{index}]"
            errors.extend(_finding_errors(finding, path, expected_items))
            if isinstance(finding, dict) and _is_text(finding.get("item_id")):
                seen.append(finding["item_id"])
            if (
                category == "intentional_adapters"
                and isinstance(finding, dict)
                and finding.get("disposition") != "intentional"
            ):
                errors.append(f"{path}.disposition must be 'intentional'")
            if (
                category == "unresolved_design_dispositions"
                and isinstance(finding, dict)
                and finding.get("disposition") != "unresolved"
            ):
                errors.append(f"{path}.disposition must be 'unresolved'")
    duplicates = sorted({item_id for item_id in seen if seen.count(item_id) > 1})
    if duplicates:
        errors.append(f"partition items classified more than once: {duplicates}")
    missing_items = sorted(set(expected_items) - set(seen))
    if missing_items:
        errors.append(f"partition items not classified: {missing_items}")
    return errors


def _partition_path(results_dir: str | Path, partition_id: str) -> Path:
    return Path(results_dir) / "partitions" / f"{partition_id}.json"


def _source_refs(partition: dict[str, Any]) -> list[dict[str, Any]]:
    by_digest: dict[str, dict[str, Any]] = {}
    for item in partition["items"]:
        for ref in item["source_refs"]:
            by_digest[_digest(ref)] = ref
    return [by_digest[key] for key in sorted(by_digest)]


def _valid_completed_envelope(
    envelope: Any, plan: dict[str, Any], partition: dict[str, Any]
) -> bool:
    return isinstance(envelope, dict) and not _validate_envelope(envelope, plan, partition)


def register_review_round(
    plan: dict[str, Any],
    arms: list[str],
    *,
    date: str | None = None,
    executing_agent: str | None = None,
    conn=None,
) -> dict[str, Any]:
    """Register one corpus review as ONE research subject, and return what happened.

    THE LARGEST UNTAPPED SIGNAL IN THE SYSTEM. A corpus review fans a scope out to several agents
    over many partitions -- structurally the same shape as a UX-review panel, which IS captured --
    but nothing bound those offloads together, so each partition was an unrelated run against an
    ephemeral temp path and thousands of offload runs across six agents produced nothing the
    learner could compare.

    Identity: the target is `domain/<review_id>` because a corpus review is not cut from a commit
    (which is exactly why base_sha is inapplicable here), and the spec is the objective PINNED to
    `plan_sha256` -- so two reviews asking the same question of different corpora are different
    subjects, and a resumed run of the same plan is the same subject.

    NEVER PADS THE ARM SET, AND A DECLARATION IS NOT EVIDENCE. `run_plan` dispatches every
    partition to ONE agent -- `--agent` -- so `--round-agents gemini,cursor` used to register a
    two-arm subject in which cursor had no round-bound attempt at all. That is false comparative
    evidence: it makes one agent's opinion look like a panel's agreement, and
    `resolve_round_durability` would later hand an issue's durability to an agent that never
    reviewed anything.

    So an arm is admitted only on a RECORD, of which there are exactly three:

    * it is the agent this invocation is about to dispatch (`executing_agent`), or
    * it already has a run bound to this round by `experiment_id` -- which is how a round genuinely
      worked by several seats is registered: one `run` invocation per agent, each landing on the
      same round id, each admitting the seats that came before it, or
    * it was already registered on this round, so the set can only GROW. A resumed single-seat run
      must not shrink the set back and orphan the other seats' findings -- `record_finding_issue`
      validates against exactly this set, so a shrink would start refusing real attributions.

    Declared arms with none of those three are DROPPED and named in `arms_unproven`. Dropping beats
    refusing: refusing would lose the round binding for the work actually being done, which is the
    real signal, over a caller's optimism about seats that had not started.

    Returns a dict, never raises for a Brain problem: capture is subordinate to the review itself,
    so a registration failure is REPORTED in the summary rather than losing the review. It is never
    silent -- an unregistered round says so by name.
    """
    import research_subjects

    review_id = str(plan.get("review_id") or "").strip()
    if not review_id:
        return {"registered": False, "reason": "plan has no review_id"}
    declared = [str(a).strip() for a in arms if str(a).strip()]
    executing = str(executing_agent or "").strip()
    if not declared and not executing:
        return {"registered": False, "reason": "no agent actually did the work"}
    spec = f"{plan.get('objective') or ''}\n\nplan_sha256:{plan.get('plan_sha256') or ''}"
    try:
        round_id = research_subjects.research_round_id(
            research_subjects.domain_target(review_id),
            "review-corpus",
            date or time.strftime("%Y-%m-%d"),
        )
        # The round id is PURE, so the evidence for who is already in this round can be read
        # before anything is written. Registration then never has to guess.
        evidence = research_subjects.round_arm_evidence(round_id, conn=conn)
        proven = {str(a).strip().lower() for a in evidence["arms"]}
        arm_list = [a for a in declared if a.lower() in proven or a.lower() == executing.lower()]
        if executing and executing.lower() not in {a.lower() for a in arm_list}:
            arm_list.append(executing)
        for extra in evidence["arms"]:
            if extra not in {a.lower() for a in arm_list}:
                arm_list.append(extra)
        unproven = [a for a in declared if a.lower() not in {b.lower() for b in arm_list}]
        if not arm_list:
            return {"registered": False, "reason": "no agent actually did the work"}
        round_id, identity = research_subjects.record_research_round(
            research_subjects.domain_target(review_id),
            "review-corpus",
            date or time.strftime("%Y-%m-%d"),
            spec,
            arm_list,
            task_type="review",
            conn=conn,
        )
    except Exception as exc:  # noqa: BLE001 - reported, never fatal to the review
        return {"registered": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {
        "registered": True,
        "round_id": round_id,
        "subject_id": identity["subject_id"],
        "arms": arm_list,
        # NAMED, NEVER SILENT: a declared arm that no record supports is reported here and in the
        # run summary, so a mis-typed `--round-agents` is visible rather than absorbed.
        "arms_unproven": unproven,
        "executing_agent": executing or None,
    }


def run_plan(
    plan: dict[str, Any],
    *,
    agent: str,
    cwd: str | Path,
    results_dir: str | Path,
    timeout: int = DEFAULT_PARTITION_TIMEOUT,
    resume: bool = True,
    offload_fn: Callable[..., dict[str, Any]] | None = None,
    round_agents: list[str] | None = None,
    round_date: str | None = None,
) -> dict[str, Any]:
    errors = validate_plan(plan)
    if errors:
        raise ValueError("invalid plan: " + "; ".join(errors))
    if timeout < 1:
        raise ValueError("timeout must be >= 1 second")
    if offload_fn is None:
        import dispatcher

        offload_fn = dispatcher.offload
    results_root = Path(results_dir)
    # Register BEFORE the first offload: the round id has to exist to be stamped onto the attempts.
    round_info = register_review_round(
        plan, round_agents or [agent], date=round_date, executing_agent=agent
    )
    research_round = round_info.get("round_id")
    statuses: list[dict[str, Any]] = []
    for partition in plan["partitions"]:
        result_path = _partition_path(results_root, partition["partition_id"])
        if resume and result_path.exists():
            try:
                existing = _read_json(result_path)
            except (OSError, json.JSONDecodeError):
                existing = None
            if _valid_completed_envelope(existing, plan, partition):
                statuses.append(
                    {
                        "partition_id": partition["partition_id"],
                        "status": "complete",
                        "reused": True,
                    }
                )
                continue
        prompt = build_partition_prompt(plan, partition)
        try:
            offload = offload_fn(
                agent,
                prompt,
                cwd=str(cwd),
                timeout=timeout,
                isolate=False,
                research_round=research_round,
            )
        except Exception as exc:  # one bad lane must not erase the remaining partition evidence
            offload = {"agent": agent, "exit": 70, "output": "", "error": f"offload raised: {exc}"}
        validation_errors: list[str] = []
        parsed: dict[str, Any] | None = None
        status = "failed"
        failure_reason = offload.get("error") or (
            f"agent exited {offload.get('exit')}" if offload.get("exit") else None
        )
        if offload.get("exit") == 0 and not offload.get("error"):
            try:
                parsed = _parse_json_object(offload.get("output", ""))
                validation_errors = validate_partition_result(parsed, plan, partition)
                status = "complete" if not validation_errors else "invalid"
                failure_reason = (
                    None if status == "complete" else "partition result failed schema validation"
                )
            except (ValueError, json.JSONDecodeError) as exc:
                status = "invalid"
                validation_errors = [f"could not parse strict JSON result: {exc}"]
                failure_reason = "partition result was not valid JSON"
        provenance = {
            "agent": offload.get("agent", agent),
            "model": offload.get("model"),
            "run_id": offload.get("run_id"),
            "log": offload.get("log"),
            "exit": offload.get("exit"),
            "attempts": offload.get("attempts"),
            "timeout_s": timeout,
            "source_refs": _source_refs(partition),
        }
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "review_id": plan["review_id"],
            "plan_sha256": plan["plan_sha256"],
            "partition_id": partition["partition_id"],
            "partition_digest": partition["partition_digest"],
            "status": status,
            "failure_reason": failure_reason,
            "validation_errors": validation_errors,
            "provenance": provenance,
            "result": parsed,
        }
        _atomic_json(result_path, envelope)
        statuses.append(
            {"partition_id": partition["partition_id"], "status": status, "reused": False}
        )
    incomplete = [row["partition_id"] for row in statuses if row["status"] != "complete"]
    summary = {
        "schema_version": SCHEMA_VERSION,
        "review_id": plan["review_id"],
        "plan_sha256": plan["plan_sha256"],
        "expected_partition_ids": [p["partition_id"] for p in plan["partitions"]],
        "partition_statuses": statuses,
        "coverage_status": "complete" if not incomplete else "incomplete",
        "incomplete_partition_ids": incomplete,
        # Reported beside the coverage it accompanies. An unregistered round is visible here by
        # name instead of being an absence nobody notices -- the failure mode this repo is named
        # after.
        "research_round": round_info,
    }
    _atomic_json(results_root / "run-summary.json", summary)
    return summary


def _validate_envelope(envelope: Any, plan: dict[str, Any], partition: dict[str, Any]) -> list[str]:
    if not isinstance(envelope, dict):
        return ["envelope must be a JSON object"]
    errors: list[str] = []
    if envelope.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"envelope schema_version must equal {SCHEMA_VERSION}")
    if envelope.get("plan_sha256") != plan["plan_sha256"]:
        errors.append("plan_sha256 mismatch")
    if envelope.get("partition_id") != partition["partition_id"]:
        errors.append("partition_id mismatch")
    if envelope.get("partition_digest") != partition["partition_digest"]:
        errors.append("partition_digest mismatch")
    if envelope.get("status") != "complete":
        errors.append(f"partition status is {envelope.get('status')!r}, not complete")
    errors.extend(validate_partition_result(envelope.get("result"), plan, partition))
    provenance = envelope.get("provenance")
    if (
        not isinstance(provenance, dict)
        or not _is_text(provenance.get("run_id"))
        or not _is_text(provenance.get("log"))
    ):
        errors.append("completed partition requires provenance.run_id and provenance.log")
    return errors


def _adjudication_queue(categories: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    by_assertion: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for category, findings in categories.items():
        for finding in findings:
            by_assertion[finding["assertion_key"]].append({"category": category, **finding})
    conflicts: list[dict[str, Any]] = []
    corroborated: list[str] = []
    for assertion_key, findings in sorted(by_assertion.items()):
        signatures = {(row["category"], row["disposition"]) for row in findings}
        if len(findings) > 1 and len(signatures) > 1:
            conflicts.append({"assertion_key": assertion_key, "findings": findings})
        elif len(findings) > 1:
            corroborated.append(assertion_key)
    unresolved = [
        finding
        for finding in categories["unresolved_design_dispositions"]
        if finding["disposition"] == "unresolved"
    ]
    return {
        "status": "needed" if conflicts or unresolved else "not_needed",
        "conflicts": conflicts,
        "unresolved_design_dispositions": unresolved,
        "corroborated_assertion_keys": corroborated,
    }


def _adjudicator_prompt(synthesis: dict[str, Any]) -> str:
    queue = synthesis["adjudication"]
    keys = sorted(
        {row["assertion_key"] for row in queue["conflicts"]}
        | {row["assertion_key"] for row in queue["unresolved_design_dispositions"]}
    )
    shape = {
        "schema_version": SCHEMA_VERSION,
        "review_id": synthesis["review_id"],
        "plan_sha256": synthesis["plan_sha256"],
        "decisions": [
            {
                "assertion_key": key,
                "decision": "uphold_finding|reject_false_positive|needs_more_evidence|owner_disposition_required",
                "rationale": "evidence-led reason",
                "evidence_refs": ["stable source reference"],
            }
            for key in keys
        ],
    }
    return (
        "Adjudicate only the normalized cross-partition conflicts and unresolved design dispositions "
        "below. Inspect cited sources in the workspace when needed. Raw name scans cannot establish a "
        "defect. This is advisory: preserve owner-required design decisions and return STRICT JSON only.\n\n"
        f"Queue:\n{json.dumps(queue, indent=2, sort_keys=True)}\n\n"
        f"Required shape:\n{json.dumps(shape, indent=2, sort_keys=True)}"
    )


def _validate_adjudication(value: Any, synthesis: dict[str, Any]) -> list[str]:
    if not isinstance(value, dict):
        return ["adjudication must be a JSON object"]
    errors: list[str] = []
    if set(value) != {"schema_version", "review_id", "plan_sha256", "decisions"}:
        errors.append(
            "adjudication must contain exactly schema_version, review_id, plan_sha256, decisions"
        )
        return errors
    if value.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"adjudication.schema_version must equal {SCHEMA_VERSION}")
    if (
        value.get("review_id") != synthesis["review_id"]
        or value.get("plan_sha256") != synthesis["plan_sha256"]
    ):
        errors.append("adjudication provenance does not match the synthesis")
    expected = {row["assertion_key"] for row in synthesis["adjudication"]["conflicts"]} | {
        row["assertion_key"] for row in synthesis["adjudication"]["unresolved_design_dispositions"]
    }
    decisions = value.get("decisions")
    if not isinstance(decisions, list):
        return errors + ["adjudication.decisions must be a list"]
    seen: list[str] = []
    for index, decision in enumerate(decisions):
        path = f"adjudication.decisions[{index}]"
        if not isinstance(decision, dict) or set(decision) != {
            "assertion_key",
            "decision",
            "rationale",
            "evidence_refs",
        }:
            errors.append(f"{path} has an invalid shape")
            continue
        assertion_key = decision.get("assertion_key")
        if assertion_key is not None:
            seen.append(assertion_key)
        if decision.get("assertion_key") not in expected:
            errors.append(f"{path}.assertion_key is not in the adjudication queue")
        if decision.get("decision") not in ADJUDICATION_DECISIONS:
            errors.append(f"{path}.decision must be one of {sorted(ADJUDICATION_DECISIONS)}")
        if not _is_text(decision.get("rationale")):
            errors.append(f"{path}.rationale must be a non-empty string")
        errors.extend(
            _string_list(decision.get("evidence_refs"), f"{path}.evidence_refs", nonempty=True)
        )
    if set(seen) != expected or len(seen) != len(set(seen)):
        errors.append("adjudication must decide every queued assertion exactly once")
    return errors


def synthesize_results(
    plan: dict[str, Any],
    *,
    results_dir: str | Path,
    adjudicator_agent: str | None = None,
    cwd: str | Path = ".",
    timeout: int = DEFAULT_PARTITION_TIMEOUT,
    offload_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    errors = validate_plan(plan)
    if errors:
        raise ValueError("invalid plan: " + "; ".join(errors))
    if timeout < 1:
        raise ValueError("timeout must be >= 1 second")
    categories: dict[str, list[dict[str, Any]]] = _empty_categories()
    partition_statuses: list[dict[str, Any]] = []
    provenance: list[dict[str, Any]] = []
    for partition in plan["partitions"]:
        path = _partition_path(results_dir, partition["partition_id"])
        if not path.exists():
            partition_statuses.append(
                {
                    "partition_id": partition["partition_id"],
                    "status": "missing",
                    "errors": ["result envelope missing"],
                }
            )
            continue
        try:
            envelope = _read_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            partition_statuses.append(
                {
                    "partition_id": partition["partition_id"],
                    "status": "invalid",
                    "errors": [str(exc)],
                }
            )
            continue
        envelope_errors = _validate_envelope(envelope, plan, partition)
        if envelope_errors:
            partition_statuses.append(
                {
                    "partition_id": partition["partition_id"],
                    "status": "invalid",
                    "errors": envelope_errors,
                }
            )
            continue
        partition_statuses.append(
            {"partition_id": partition["partition_id"], "status": "complete", "errors": []}
        )
        provenance.append({"partition_id": partition["partition_id"], **envelope["provenance"]})
        for category in CATEGORY_KEYS:
            for finding in envelope["result"]["categories"][category]:
                categories[category].append({"partition_id": partition["partition_id"], **finding})
    incomplete = [row["partition_id"] for row in partition_statuses if row["status"] != "complete"]
    adjudication = _adjudication_queue(categories)
    remaining = [
        row
        for findings in categories.values()
        for row in findings
        if row["disposition"] in {"remaining", "partial"}
    ]
    if incomplete:
        verdict = "INCOMPLETE"
    elif adjudication["status"] == "needed":
        verdict = "NEEDS_ADJUDICATION"
    elif remaining:
        verdict = "FINDINGS_REMAIN"
    else:
        verdict = "COMPLETE"
    synthesis: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "review_id": plan["review_id"],
        "plan_sha256": plan["plan_sha256"],
        "coverage_status": "incomplete" if incomplete else "complete",
        "verdict": verdict,
        "partition_statuses": partition_statuses,
        "categories": categories,
        "remaining_findings": remaining,
        "adjudication": adjudication,
        "partition_provenance": provenance,
        "adjudicator": None,
    }
    if adjudicator_agent and incomplete:
        synthesis["adjudicator"] = {
            "status": "skipped_incomplete_coverage",
            "errors": ["adjudication requires complete partition coverage"],
            "result": None,
            "provenance": None,
        }
    elif adjudicator_agent and adjudication["status"] == "needed":
        if offload_fn is None:
            import dispatcher

            offload_fn = dispatcher.offload
        try:
            response = offload_fn(
                adjudicator_agent,
                _adjudicator_prompt(synthesis),
                cwd=str(cwd),
                timeout=timeout,
                isolate=False,
            )
        except Exception as exc:
            response = {
                "agent": adjudicator_agent,
                "exit": 70,
                "output": "",
                "error": f"offload raised: {exc}",
            }
        parsed = None
        adjudication_errors: list[str] = []
        if response.get("exit") == 0 and not response.get("error"):
            try:
                parsed = _parse_json_object(response.get("output", ""))
                adjudication_errors = _validate_adjudication(parsed, synthesis)
            except (ValueError, json.JSONDecodeError) as exc:
                adjudication_errors = [f"could not parse strict JSON adjudication: {exc}"]
        else:
            adjudication_errors = [response.get("error") or f"agent exited {response.get('exit')}"]
        synthesis["adjudicator"] = {
            "status": "complete" if not adjudication_errors else "failed",
            "errors": adjudication_errors,
            "result": parsed,
            "provenance": {
                "agent": response.get("agent", adjudicator_agent),
                "model": response.get("model"),
                "run_id": response.get("run_id"),
                "log": response.get("log"),
                "exit": response.get("exit"),
                "attempts": response.get("attempts"),
                "timeout_s": timeout,
            },
        }
    return synthesis


def _sample_finding(
    item: dict[str, Any], *, category: str = "removed_product_surfaces"
) -> dict[str, Any]:
    disposition = (
        "intentional"
        if category == "intentional_adapters"
        else ("unresolved" if category == "unresolved_design_dispositions" else "satisfied")
    )
    return {
        "item_id": item["item_id"],
        "assertion_key": item["assertion_key"],
        "disposition": disposition,
        "summary": "Verified against current code and history.",
        "evidence": [
            {
                "type": "current_code",
                "ref": "src/example.py:1",
                "observation": "Current implementation inspected.",
            }
        ],
        "confidence": "high",
        "recommended_action": None,
    }


def _selftest() -> None:
    corpus = {
        "schema_version": 1,
        "review_id": "selftest-review",
        "objective": "Reconcile source-PR feedback against current code.",
        "shared_context": ["Names alone are not defects."],
        "items": [
            {
                "item_id": f"CW-{index}",
                "assertion_key": f"surface-{index}",
                "group_key": "source-pr-61" if index < 3 else "source-pr-62",
                "assertion": f"Verify surface {index}",
                "source_refs": [
                    {"kind": "pull_request", "ref": f"owner/repo#{61 if index < 3 else 62}"}
                ],
            }
            for index in range(5)
        ],
    }
    plan = partition_corpus(corpus, max_items=2, max_prompt_chars=12_000)
    assert [len(p["items"]) for p in plan["partitions"]] == [2, 1, 2], plan
    assert not validate_plan(plan), validate_plan(plan)
    partition = plan["partitions"][0]
    result = {
        "schema_version": 1,
        "review_id": plan["review_id"],
        "partition_id": partition["partition_id"],
        "partition_digest": partition["partition_digest"],
        "categories": _empty_categories(),
        "summary": "Done.",
    }
    result["categories"]["removed_product_surfaces"] = [
        _sample_finding(item) for item in partition["items"]
    ]
    assert not validate_partition_result(result, plan, partition)
    # Deliberate break: a name scan alone cannot turn discovery into a confirmed result.
    broken = json.loads(json.dumps(result))
    broken["categories"]["removed_product_surfaces"][0]["evidence"][0]["type"] = "name_scan"
    assert any(
        "raw name scans" in error for error in validate_partition_result(broken, plan, partition)
    )
    assert not validate_partition_result(result, plan, partition), "reverted result must validate"
    with tempfile.TemporaryDirectory(prefix="partitioned-review-") as tmp:
        incomplete = synthesize_results(plan, results_dir=tmp)
        assert (
            incomplete["verdict"] == "INCOMPLETE" and incomplete["coverage_status"] == "incomplete"
        )
    print(
        "partitioned_review.py selftest: OK (bounded groups, strict categories, name-scan break/revert, fail-closed missing partitions)"
    )


DISABLED = os.environ.get("ORCH_PARTITIONED_REVIEW_DISABLED", "").strip() == "1"


def _capability_heartbeat(event_type: str = "invocation") -> None:
    """Credit this capability when it actually runs.

    It shipped (PR #2) with no ledger record, no heartbeat and a CLI-only caller — the
    built-and-forgotten shape that is this project's dominant defect class. Without a heartbeat the
    firing monitor can only ever report it as never-fired, so no amount of real use would show up
    and the module would eventually read as dead code worth deleting.

    Deliberately best-effort: an observability failure must never break a review run.
    """
    try:
        import capabilities

        capabilities.production_heartbeat(
            "partitioned-review", event_type, ref="partitioned_review.main"
        )
    except Exception:  # noqa: BLE001
        pass


def main(
    argv: list[str] | None = None, *, offload_fn: Callable[..., dict[str, Any]] | None = None
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--selftest", action="store_true")
    sub = parser.add_subparsers(dest="command")
    prepare = sub.add_parser("prepare", help="partition a structured corpus into a bounded plan")
    prepare.add_argument("--corpus", required=True)
    prepare.add_argument("--plan", required=True)
    prepare.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS)
    prepare.add_argument("--max-prompt-chars", type=int, default=DEFAULT_MAX_PROMPT_CHARS)
    run = sub.add_parser("run", help="run every partition through dispatcher.offload")
    run.add_argument("--plan", required=True)
    run.add_argument("--results-dir", required=True)
    run.add_argument("--agent", required=True)
    run.add_argument("--cwd", default=".")
    run.add_argument("--timeout", type=int, default=DEFAULT_PARTITION_TIMEOUT)
    run.add_argument("--no-resume", action="store_true")
    # A round fanned out to several seats is several ARMS of one subject, reached by running this
    # command once per seat against the same `--round-date`. The list is a DECLARATION and is
    # corroborated, not believed: this invocation dispatches only `--agent`, so any other name is
    # admitted only once it has a run bound to the round, and is otherwise dropped and reported as
    # `arms_unproven`. A forged arm set would make one agent's opinion look like a panel's.
    run.add_argument(
        "--round-agents",
        help="comma-separated agents working this round; only those with a round-bound run (plus "
        "--agent) are registered as arms (default: --agent alone)",
    )
    # Given explicitly when resuming a round started on an earlier day, so the resumed run lands
    # on the SAME subject rather than creating a second one.
    run.add_argument("--round-date", help="YYYY-MM-DD of the round (default: today)")
    synth = sub.add_parser(
        "synthesize", help="fail-closed synthesis and optional advisory adjudication"
    )
    synth.add_argument("--plan", required=True)
    synth.add_argument("--results-dir", required=True)
    synth.add_argument("--output", required=True)
    synth.add_argument("--adjudicator-agent")
    synth.add_argument("--cwd", default=".")
    synth.add_argument("--timeout", type=int, default=DEFAULT_PARTITION_TIMEOUT)
    args = parser.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    # Kill switch. This module is reached only through an explicit dispatcher subcommand, so not
    # invoking it is already a full stop; the flag exists so an operator can disable it fleet-wide
    # without editing code, which is what the admission gate means by "nothing can stop it".
    if DISABLED:
        print(json.dumps({"skipped": "ORCH_PARTITIONED_REVIEW_DISABLED=1"}, sort_keys=True))
        return 0
    _capability_heartbeat()
    if args.command == "prepare":
        plan = partition_corpus(
            _read_json(args.corpus),
            max_items=args.max_items,
            max_prompt_chars=args.max_prompt_chars,
        )
        _atomic_json(args.plan, plan)
        print(
            json.dumps(
                {
                    "plan": args.plan,
                    "plan_sha256": plan["plan_sha256"],
                    "partitions": len(plan["partitions"]),
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "run":
        summary = run_plan(
            _read_json(args.plan),
            agent=args.agent,
            cwd=args.cwd,
            results_dir=args.results_dir,
            timeout=args.timeout,
            round_agents=[a.strip() for a in (args.round_agents or "").split(",") if a.strip()]
            or None,
            round_date=args.round_date,
            resume=not args.no_resume,
            offload_fn=offload_fn,
        )
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0 if summary["coverage_status"] == "complete" else 1
    if args.command == "synthesize":
        synthesis = synthesize_results(
            _read_json(args.plan),
            results_dir=args.results_dir,
            adjudicator_agent=args.adjudicator_agent,
            cwd=args.cwd,
            timeout=args.timeout,
            offload_fn=offload_fn,
        )
        _atomic_json(args.output, synthesis)
        print(
            json.dumps(
                {
                    "output": args.output,
                    "coverage_status": synthesis["coverage_status"],
                    "verdict": synthesis["verdict"],
                },
                sort_keys=True,
            )
        )
        return 0 if synthesis["verdict"] == "COMPLETE" else 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
