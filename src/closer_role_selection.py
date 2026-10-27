"""Reviewed quality/scope filter for the existing neutral Closer router.

This file never enrolls a runtime, calls a provider, or authorizes an action.
Categories are chosen by the trusted host for its intended use, not by a model's
reported finding subtype. Unknown or unsupported categories yield no candidate.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import paths

POLICY_PATH = paths.REPO_ROOT / "config" / "closer_role_selection.json"
SCHEMA = "closer-role-selection/v1"


def _unique(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate selection policy key")
        result[key] = value
    return result


def _policy_bytes(path: Path | None = None) -> bytes:
    source = path or POLICY_PATH
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 1_048_576:
            raise ValueError("selection policy physical source unavailable")
        raw = os.read(fd, info.st_size + 1)
    finally:
        os.close(fd)
    if len(raw) != info.st_size:
        raise ValueError("selection policy changed during read")
    return raw


def policy_sha256() -> str:
    """Bind the installed policy bytes into the controlled source contract."""
    return hashlib.sha256(_policy_bytes()).hexdigest()


def load_policy(path: Path | None = None) -> dict[str, Any]:
    """Read committed selection decisions; external evidence is not fetched as authority."""
    value = json.loads(_policy_bytes(path), object_pairs_hook=_unique)
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ValueError("unsupported Closer selection policy")
    for key in ("default_categories", "subjects", "categories", "fallback_rules"):
        if not isinstance(value.get(key), dict):
            raise ValueError("incomplete Closer selection policy")
    if not isinstance(value.get("policy_version"), str) or not value["policy_version"]:
        raise ValueError("selection policy lacks a version")
    return value


def category_spec(role: str, category: str | None, policy: Mapping[str, Any]):
    category = category if category is not None else policy["default_categories"].get(role)
    spec = policy["categories"].get(category) if isinstance(category, str) else None
    if not isinstance(spec, dict) or spec.get("role") != role:
        return None
    for key in ("candidates", "requirements"):
        values = spec.get(key)
        if not isinstance(values, list) or any(not isinstance(x, str) for x in values):
            return None
        if len(values) != len(set(values)):
            return None
    if not isinstance(spec.get("terminal_fallback"), str):
        return None
    return category, spec


def quality_scope(
    profile: Mapping[str, Any], role: str, category: str | None = None, *, policy=None
):
    """Return scope metadata only for an exact, explicitly bound quality subject.

    A missing profile_id is a pending integration decision, never a wildcard.
    This predicate is necessary but insufficient: callers must also use existing
    runtime qualification, operator-cap, capacity and transport checks.
    """
    policy = load_policy() if policy is None else policy
    found = category_spec(role, category, policy)
    if found is None:
        return None
    category, spec = found
    matches = []
    for key in spec["candidates"]:
        subject = policy["subjects"].get(key)
        if not isinstance(subject, dict) or subject.get("quality_status") != "guarded_eligible":
            continue
        profile_id = subject.get("profile_id")
        identity = subject.get("identity")
        if (
            not profile_id
            or profile_id != profile.get("profile_id")
            or not isinstance(identity, dict)
        ):
            continue
        if set(identity) != {
            "agent",
            "provider",
            "requested_model",
            "reasoning_effort",
        }:
            continue
        if any(profile.get(name) != value for name, value in identity.items()):
            continue
        if not isinstance(subject.get("limits"), list) or any(
            not isinstance(x, str) for x in subject["limits"]
        ):
            continue
        if subject.get("runtime_enrollment_granted") is not False:
            continue
        matches.append((key, subject))
    if len(matches) != 1:
        return None
    key, subject = matches[0]
    return {
        "policy_version": policy["policy_version"],
        "category": category,
        "subject": key,
        "independence_group": subject.get("independence_group"),
        "quality_evidence": subject["quality_evidence"],
        "requirements": sorted(set(spec["requirements"] + subject["limits"])),
        "terminal_fallback": spec["terminal_fallback"],
        "runtime_enrollment_granted": False,
        "action_authority": False,
    }


def fallback_decision(role, category, failure, *, cleanup_terminal=False, policy=None):
    """Describe the existing hold/reselection path; never dispatch or retry here."""
    policy = load_policy() if policy is None else policy
    found = category_spec(role, category, policy)
    if found is None:
        return {"action": "collect_or_independent_review_hold", "dispatch": False}
    action = policy["fallback_rules"].get(failure, "collect_or_independent_review_hold")
    if failure == "known_answer_rejected" and not cleanup_terminal:
        action = "hold_do_not_repeat"
    return {
        "action": action,
        "category": found[0],
        "dispatch": False,
        "new_runtime_and_budget_admission_required": action.startswith("reselect_"),
        "action_authority": False,
    }


def candidates_for_agent(
    agent, role, transport, category=None, *, policy=None, independent_of_provider=None
):
    """Enumerate only reviewed exact bindings, including role-scoped explicit opt-ins."""
    import execution_profiles

    policy = load_policy() if policy is None else policy
    if independent_of_provider is not None:
        known_providers = {row["provider"] for row in execution_profiles.CAPACITY_POOLS.values()}
        if (
            not isinstance(independent_of_provider, str)
            or independent_of_provider not in known_providers
        ):
            return []
    result = []
    for profile in execution_profiles.profiles_for_agent(
        agent, transport=transport, include_explicit=True
    ):
        scope = quality_scope(profile, role, category, policy=policy)
        if scope is None:
            continue
        group = scope["independence_group"]
        if independent_of_provider is not None and (
            group is None or group == independent_of_provider
        ):
            continue
        result.append(profile)
    return result
