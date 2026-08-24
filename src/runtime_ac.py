#!/usr/bin/env python3
"""Build, run, and gate Orchestrator runtime acceptance-criteria verification specs.

The runtime_ac lane is a first increment for richer runtime AC checks. It turns
goals into structured evidence plans, validates those plans, and emits
review-before-run commands that route frontend checks through frontend_verify.py
and deliberate-break checks through local_verify.py. Execution is opt-in via
--confirm-run and never mutates repositories.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import feedback

ORCH_DIR = Path(__file__).resolve().parent
TAIL_CHARS = 4000

VALID_CHECK_TYPES = {"command", "frontend", "deliberate_break", "manual"}
VALID_EVIDENCE_TYPES = {
    "runtime_behavior",
    "test_output",
    "deliberate_break",
    "command_output",
    "manual_review",
}
VALID_RISK_LEVELS = {"low", "medium", "high"}
VALID_EXPECTED = {"exit_0", "contains", "regex", "manual_review"}
MUTATING_FLAGS = {"--apply", "--write", "--in-place", "--inplace", "--fix", "--force"}
MUTATING_GIT_SUBCOMMANDS = {
    "reset",
    "checkout",
    "restore",
    "clean",
    "commit",
    "push",
    "merge",
    "rebase",
}
MUTATING_GH_PR_SUBCOMMANDS = {"create", "merge", "close", "edit", "ready"}
SHELL_REVIEW_MARKERS = ("|", "&&", "||", ";", ">", "<", "`", "$(")
SHELL_EXPANSION_MARKERS = ("*", "$", "~")

# ORCH-ANCHOR: runtime-ac-command-exec-gate
# WHICH check types ORCH_RUNTIME_AC_ALLOW_COMMANDS decides. Named ONCE so the runtime gate and the
# recurrence fixture that reports on that switch consume the same name — a shared name cannot drift,
# a matching pair of literals will.
#
# `command` and `non_regression` carry a `command` string authored ENTIRELY by the agent and handed
# to `shlex.split`, so they are opt-in. `deliberate_break` is deliberately NOT here and never was:
# its executed command is built from a template by `_local_verify_command`, and its agent-authored
# payload is `test_cmd`, which local_verify runs with `shlex.split` + `shell=False` only after
# `_has_shell_marker` has rejected every shell control character.
#
# THIS MATTERS BECAUSE A STORED VERDICT SAID OTHERWISE. The recorded criterion for
# ORCH_RUNTIME_AC_ALLOW_COMMANDS said it gated BOTH kinds and that the flag had to be SPLIT before
# it could be turned on, which is why `deliberate-break-verifier` sat in the "held switches" bucket.
# The split already existed — right here. Measured 2026-08-22 by running a real deliberate_break
# spec: identical results with allow_command_checks False and True. That was a frozen diagnosis, not
# a blocker, and the honest fix is to name the split rather than to add a second gate (a new
# default-OFF gate over a path that currently works would create the latch, not remove one).
COMMAND_EXEC_GATED_TYPES = frozenset({"command", "non_regression"})


def command_execution_gated(check_type: str | None) -> bool:
    """Does ORCH_RUNTIME_AC_ALLOW_COMMANDS decide whether this check type may execute?"""
    return check_type in COMMAND_EXEC_GATED_TYPES


BLOCKING_STATUSES = {"FAIL", "ERROR", "UNSAFE"}
INCOMPLETE_STATUSES = {"MISSING", "NEEDS_REVIEW", "SKIP"}
VERDICT_BY_GATE = {
    "PASS": "PASS_RUNTIME_AC",
    "FAIL": "FAIL_RUNTIME_AC",
    "NEEDS_REVIEW": "NEEDS_REVIEW_RUNTIME_AC",
}


def capture_evidence_contract(plan: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Typed capture hook; runtime execution output is never retained here."""
    if plan.get("capture_hook") != "runtime_ac.named_test_capture":
        raise ValueError("plan does not target the runtime_ac capture hook")
    from capability_compiler import capture_named_test_evidence

    return capture_named_test_evidence(plan, result)


RUNTIME_AC_SCHEMA_EXAMPLE = {
    "verification": {
        "id": "course-progress-runtime-ac",
        "title": "Course progress runtime verification",
        "target": "owner/repo#123",
        "repo": "owner/repo",
        "goal": "Verify learners and instructors see accurate course progress.",
        "risk_level": "medium",
    },
    "runtime_context": {
        "worktree": ".",
        "serve_command": "npm run dev",
        "health_url": "http://localhost:3000/health",
        "base_ref": "origin/main",
        "browser_endpoint": None,
        "notes": ["Start the dev server before frontend checks."],
    },
    "acceptance_criteria": [
        {
            "id": "AC1",
            "statement": "Learners can see percent-complete progress on the course page.",
            "evidence_required": ["runtime_behavior", "test_output", "deliberate_break"],
            "checks": [
                {
                    "id": "AC1-FE",
                    "type": "frontend",
                    "name": "Course page progress text",
                    "url": "http://localhost:3000/courses/intro",
                    "assertions": ["text:Progress", "role:progressbar"],
                },
                {
                    "id": "AC1-TEST",
                    "type": "command",
                    "name": "Course progress tests",
                    "command": "pytest tests/test_course_progress.py",
                    "expected": "exit_0",
                },
                {
                    "id": "AC1-BREAK",
                    "type": "deliberate_break",
                    "name": "Progress tests fail against base implementation",
                    "worktree": ".",
                    "base_ref": "origin/main",
                    "test_cmd": "pytest tests/test_course_progress.py",
                    "test_paths": ["tests/test_course_progress.py"],
                },
            ],
        },
        {
            "id": "AC2",
            "statement": "Instructor dashboard shows the same completion value.",
            "evidence_required": ["runtime_behavior"],
            "checks": [
                {
                    "id": "AC2-FE",
                    "type": "frontend",
                    "name": "Instructor dashboard progress",
                    "url": "http://localhost:3000/instructor/courses/intro",
                    "assertions": ["text:Completion", "text:75%"],
                }
            ],
        },
    ],
    "non_regression": [
        {"name": "Focused progress suite", "command": "pytest tests/test_course_progress.py"}
    ],
    "verdict_policy": {
        "require_runtime_evidence": True,
        "require_deliberate_break_for_tests": True,
        "fail_on_missing_checks": True,
        "required_check_ids": ["AC1-FE", "AC1-TEST", "AC1-BREAK", "AC2-FE"],
        "min_pass_ratio": 1.0,
    },
}


def _read_optional(path: str | Path | None) -> str:
    if not path:
        return ""
    return Path(path).read_text(encoding="utf-8")


def build_authoring_prompt(
    *,
    goal: str,
    repo: str | None = None,
    target: str | None = None,
    context: str = "",
    context_file: str | Path | None = None,
) -> str:
    """Return a prompt for an offloaded runtime-AC specification pass."""
    if not goal.strip():
        raise ValueError("goal must be non-empty")

    file_context = _read_optional(context_file)
    context_parts = [part.strip() for part in (context, file_context) if part and part.strip()]
    context_block = (
        "\n\nAdditional context:\n" + "\n\n".join(context_parts) if context_parts else ""
    )
    repo_block = f"\nRepository: {repo}" if repo else ""
    target_block = f"\nTarget: {target}" if target else ""
    schema = json.dumps(RUNTIME_AC_SCHEMA_EXAMPLE, indent=2)

    return f"""You are in the Orchestrator runtime acceptance-criteria verification lane.

Goal:
{goal.strip()}{repo_block}{target_block}{context_block}

Turn this into a structured runtime verification spec. The output must bind each
acceptance criterion to concrete evidence, not just list generic tests. Prefer
evidence types from this set when applicable: {", ".join(sorted(VALID_EVIDENCE_TYPES))}.

Return exactly one JSON object and no prose. The object must match this shape:

{schema}

Rules:
- verification.id must be a stable lowercase slug.
- Every acceptance criterion needs a unique id, a statement, evidence_required, and at least one check.
- Check types must be one of: command, frontend, deliberate_break, manual.
- Frontend checks use frontend_verify.py assertions: "text:<substring>" or "role:<role>[=<name>]".
- If browser launch may be sandbox-blocked, set runtime_context.browser_endpoint or a frontend
  check browser_endpoint to a Chrome/Chromium CDP URL such as http://127.0.0.1:9222.
- Command checks are review-before-run and must not include destructive git/gh/rm/sudo operations.
- Deliberate-break checks must include test_cmd and test_paths for local_verify.py.
- Manual checks should be reserved for evidence that cannot be automated yet.
- verdict_policy.required_check_ids may reference only declared check ids.
- Do not include commands that create branches, commits, PRs, or merges; runtime execution is opt-in and guarded.
"""


def _is_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _looks_like_slug(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]*", value))


def _looks_like_id(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", value))


def _validate_string_list(value: Any, path: str, *, nonempty: bool) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, list):
        return [f"{path} must be a list"]
    if nonempty and not value:
        errors.append(f"{path} must be a non-empty list")
    for idx, item in enumerate(value):
        if not _is_nonempty_string(item):
            errors.append(f"{path}[{idx}] must be a non-empty string")
    return errors


def _validate_required_mapping(obj: Any, path: str, required: Sequence[str]) -> list[str]:
    if not isinstance(obj, dict):
        return [f"{path} must be an object"]
    return [f"{path}.{key} is required" for key in required if key not in obj]


def _unsafe_command_reason(command: str) -> str | None:
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        return f"cannot be parsed safely: {exc}"
    if not tokens:
        return "must not be empty"

    lowered = [token.lower() for token in tokens]
    token_set = set(lowered)
    first = lowered[0]
    second = lowered[1] if len(lowered) > 1 else ""
    third = lowered[2] if len(lowered) > 2 else ""

    if first in {"rm", "sudo", "kill", "launchctl"}:
        return f"starts with destructive or host-mutating command {tokens[0]!r}"
    if first == "git" and second in MUTATING_GIT_SUBCOMMANDS:
        return f"uses mutating git subcommand {tokens[1]!r}"
    if first == "gh" and second == "pr" and third in MUTATING_GH_PR_SUBCOMMANDS:
        return f"uses mutating gh pr subcommand {tokens[2]!r}"
    if token_set & MUTATING_FLAGS:
        return f"uses mutating flag {sorted(token_set & MUTATING_FLAGS)[0]!r}"
    return None


def _command_review_warnings(command: str, path: str) -> list[str]:
    warnings = []
    for marker in SHELL_REVIEW_MARKERS:
        if marker in command:
            warnings.append(
                f"{path} contains shell marker {marker!r}; review carefully before running"
            )
            break
    for marker in SHELL_EXPANSION_MARKERS:
        if marker in command:
            warnings.append(
                f"{path} contains shell expansion marker {marker!r}; runtime execution uses shell=False"
            )
            break
    return warnings


def _evidence_for_check(check: dict[str, Any]) -> set[str]:
    check_type = check.get("type")
    if check_type == "frontend":
        return {"runtime_behavior"}
    if check_type == "deliberate_break":
        return {"test_output", "deliberate_break"}
    if check_type == "manual":
        return {"manual_review"}
    if check_type == "command":
        command = str(check.get("command", "")).lower()
        if any(
            marker in command
            for marker in ("pytest", " unittest", "npm test", "vitest", "playwright test")
        ):
            return {"test_output"}
        return {"command_output"}
    return set()


def _validate_check(check: Any, path: str, known_check_ids: set[str]) -> tuple[list[str], set[str]]:
    errors: list[str] = []
    evidence: set[str] = set()
    errors.extend(_validate_required_mapping(check, path, ("id", "type", "name")))
    if not isinstance(check, dict):
        return errors, evidence

    check_id = check.get("id")
    if not _is_nonempty_string(check_id):
        errors.append(f"{path}.id must be a non-empty string")
    elif not _looks_like_id(str(check_id or "").strip()):
        errors.append(
            f"{path}.id must start with a letter and contain only letters, numbers, dots, underscores, or hyphens"
        )
    elif check_id in known_check_ids:
        errors.append(f"{path}.id duplicates {check_id!r}")
    else:
        known_check_ids.add(str(check_id))

    check_type = check.get("type")
    if check_type not in VALID_CHECK_TYPES:
        errors.append(f"{path}.type must be one of {sorted(VALID_CHECK_TYPES)}")
        return errors, evidence
    if not _is_nonempty_string(check.get("name")):
        errors.append(f"{path}.name must be a non-empty string")

    if check_type == "command":
        errors.extend(_validate_required_mapping(check, path, ("command", "expected")))
        command = check.get("command")
        if not _is_nonempty_string(command):
            errors.append(f"{path}.command must be a non-empty string")
        else:
            unsafe = _unsafe_command_reason(str(command or ""))
            if unsafe:
                errors.append(f"{path}.command {unsafe}")
        expected = check.get("expected")
        if expected not in VALID_EXPECTED:
            errors.append(f"{path}.expected must be one of {sorted(VALID_EXPECTED)}")
        if expected == "contains" and not _is_nonempty_string(check.get("contains")):
            errors.append(f"{path}.contains is required when expected is contains")
        if expected == "regex" and not _is_nonempty_string(check.get("pattern")):
            errors.append(f"{path}.pattern is required when expected is regex")

    if check_type == "frontend":
        errors.extend(_validate_required_mapping(check, path, ("url", "assertions")))
        if not _is_nonempty_string(check.get("url")):
            errors.append(f"{path}.url must be a non-empty string")
        errors.extend(
            _validate_string_list(check.get("assertions"), f"{path}.assertions", nonempty=True)
        )
        for idx, assertion in enumerate(check.get("assertions") or []):
            if isinstance(assertion, str) and not assertion.startswith(("text:", "role:")):
                errors.append(f"{path}.assertions[{idx}] must start with 'text:' or 'role:'")
        for optional in ("click_text", "then_text", "screenshot", "browser_endpoint"):
            if (
                optional in check
                and check[optional] is not None
                and not isinstance(check[optional], str)
            ):
                errors.append(f"{path}.{optional} must be a string or null")

    if check_type == "deliberate_break":
        errors.extend(_validate_required_mapping(check, path, ("test_cmd", "test_paths")))
        test_cmd = check.get("test_cmd")
        if not _is_nonempty_string(test_cmd):
            errors.append(f"{path}.test_cmd must be a non-empty string")
        else:
            unsafe = _unsafe_command_reason(str(test_cmd or ""))
            if unsafe:
                errors.append(f"{path}.test_cmd {unsafe}")
        errors.extend(
            _validate_string_list(check.get("test_paths"), f"{path}.test_paths", nonempty=True)
        )
        for optional in ("worktree", "base_ref"):
            if (
                optional in check
                and check[optional] is not None
                and not isinstance(check[optional], str)
            ):
                errors.append(f"{path}.{optional} must be a string or null")

    if check_type == "manual":
        errors.extend(_validate_required_mapping(check, path, ("instructions",)))
        if not _is_nonempty_string(check.get("instructions")):
            errors.append(f"{path}.instructions must be a non-empty string")

    evidence |= _evidence_for_check(check)
    return errors, evidence


def validate_spec(spec: dict[str, Any]) -> list[str]:
    """Validate an agent-produced runtime AC specification."""
    errors: list[str] = []
    if not isinstance(spec, dict):
        return ["runtime AC spec must be a JSON object"]

    required_top = ("verification", "acceptance_criteria", "verdict_policy")
    errors.extend(_validate_required_mapping(spec, "spec", required_top))
    if errors:
        return errors

    meta = spec["verification"]
    errors.extend(
        _validate_required_mapping(meta, "verification", ("id", "title", "goal", "risk_level"))
    )
    if isinstance(meta, dict):
        verification_id = meta.get("id")
        if not _is_nonempty_string(verification_id):
            errors.append("verification.id must be a non-empty string")
        elif not _looks_like_slug(str(verification_id or "").strip()):
            errors.append("verification.id must be a lowercase slug")
        for key in ("title", "goal"):
            if not _is_nonempty_string(meta.get(key)):
                errors.append(f"verification.{key} must be a non-empty string")
        for key in ("target", "repo"):
            if key in meta and meta[key] is not None and not isinstance(meta[key], str):
                errors.append(f"verification.{key} must be a string or null")
        if meta.get("risk_level") not in VALID_RISK_LEVELS:
            errors.append(f"verification.risk_level must be one of {sorted(VALID_RISK_LEVELS)}")

    runtime_context = spec.get("runtime_context") or {}
    if not isinstance(runtime_context, dict):
        errors.append("runtime_context must be an object when present")
        runtime_context = {}
    for key in ("worktree", "serve_command", "health_url", "base_ref", "browser_endpoint"):
        if (
            key in runtime_context
            and runtime_context[key] is not None
            and not isinstance(runtime_context[key], str)
        ):
            errors.append(f"runtime_context.{key} must be a string or null")
    errors.extend(
        _validate_string_list(
            runtime_context.get("notes", []), "runtime_context.notes", nonempty=False
        )
    )
    if _is_nonempty_string(runtime_context.get("serve_command")):
        unsafe = _unsafe_command_reason(runtime_context["serve_command"])
        if unsafe:
            errors.append(f"runtime_context.serve_command {unsafe}")

    criteria = spec["acceptance_criteria"]
    if not isinstance(criteria, list):
        errors.append("acceptance_criteria must be a list")
        criteria = []
    elif not criteria:
        errors.append("acceptance_criteria must be a non-empty list")

    known_ac_ids: set[str] = set()
    known_check_ids: set[str] = set()
    all_evidence: set[str] = set()
    has_deliberate_break = False
    for idx, criterion in enumerate(criteria):
        path = f"acceptance_criteria[{idx}]"
        errors.extend(
            _validate_required_mapping(
                criterion,
                path,
                ("id", "statement", "evidence_required", "checks"),
            )
        )
        if not isinstance(criterion, dict):
            continue
        ac_id = criterion.get("id")
        if not _is_nonempty_string(ac_id):
            errors.append(f"{path}.id must be a non-empty string")
        elif not _looks_like_id(str(ac_id or "").strip()):
            errors.append(
                f"{path}.id must start with a letter and contain only letters, numbers, dots, underscores, or hyphens"
            )
        elif ac_id in known_ac_ids:
            errors.append(f"{path}.id duplicates {ac_id!r}")
        else:
            known_ac_ids.add(str(ac_id))
        if not _is_nonempty_string(criterion.get("statement")):
            errors.append(f"{path}.statement must be a non-empty string")
        errors.extend(
            _validate_string_list(
                criterion.get("evidence_required"), f"{path}.evidence_required", nonempty=True
            )
        )
        checks = criterion.get("checks")
        if not isinstance(checks, list):
            errors.append(f"{path}.checks must be a list")
            continue
        if not checks:
            errors.append(f"{path}.checks must be a non-empty list")
        criterion_evidence: set[str] = set()
        for check_idx, check in enumerate(checks):
            check_errors, evidence = _validate_check(
                check, f"{path}.checks[{check_idx}]", known_check_ids
            )
            errors.extend(check_errors)
            criterion_evidence |= evidence
            all_evidence |= evidence
            if isinstance(check, dict) and check.get("type") == "deliberate_break":
                has_deliberate_break = True

        missing = sorted(
            set(criterion.get("evidence_required") or [])
            & VALID_EVIDENCE_TYPES - criterion_evidence
        )
        if missing:
            errors.append(f"{path}.checks do not provide required evidence types: {missing}")

    non_regression = spec.get("non_regression", [])
    if not isinstance(non_regression, list):
        errors.append("non_regression must be a list when present")
        non_regression = []
    for idx, entry in enumerate(non_regression):
        path = f"non_regression[{idx}]"
        if isinstance(entry, str):
            if not entry.strip():
                errors.append(f"{path} must be a non-empty string")
            elif _unsafe_command_reason(entry):
                errors.append(f"{path} command {_unsafe_command_reason(entry)}")
        elif isinstance(entry, dict):
            errors.extend(_validate_required_mapping(entry, path, ("name", "command")))
            if not _is_nonempty_string(entry.get("name")):
                errors.append(f"{path}.name must be a non-empty string")
            command = entry.get("command")
            if not _is_nonempty_string(command):
                errors.append(f"{path}.command must be a non-empty string")
            elif _unsafe_command_reason(str(command or "")):
                errors.append(f"{path}.command {_unsafe_command_reason(str(command or ''))}")
        else:
            errors.append(f"{path} must be a string command or object")

    policy = spec["verdict_policy"]
    errors.extend(
        _validate_required_mapping(
            policy,
            "verdict_policy",
            (
                "require_runtime_evidence",
                "require_deliberate_break_for_tests",
                "fail_on_missing_checks",
            ),
        )
    )
    if isinstance(policy, dict):
        for key in (
            "require_runtime_evidence",
            "require_deliberate_break_for_tests",
            "fail_on_missing_checks",
        ):
            if not isinstance(policy.get(key), bool):
                errors.append(f"verdict_policy.{key} must be a boolean")
        if policy.get("require_runtime_evidence") and "runtime_behavior" not in all_evidence:
            errors.append(
                "verdict_policy.require_runtime_evidence is true but no frontend/runtime check is present"
            )
        if policy.get("require_deliberate_break_for_tests") and not has_deliberate_break:
            errors.append(
                "verdict_policy.require_deliberate_break_for_tests is true but no deliberate_break check is present"
            )
        required_check_ids = policy.get("required_check_ids", [])
        errors.extend(
            _validate_string_list(
                required_check_ids, "verdict_policy.required_check_ids", nonempty=False
            )
        )
        for idx, check_id in enumerate(
            required_check_ids if isinstance(required_check_ids, list) else []
        ):
            if isinstance(check_id, str) and check_id not in known_check_ids:
                errors.append(
                    f"verdict_policy.required_check_ids[{idx}] references unknown check id {check_id!r}"
                )
        if "min_pass_ratio" in policy:
            ratio = policy["min_pass_ratio"]
            if not isinstance(ratio, (int, float)) or isinstance(ratio, bool):
                errors.append("verdict_policy.min_pass_ratio must be a number")
            elif not 0.0 <= float(ratio) <= 1.0:
                errors.append("verdict_policy.min_pass_ratio must be between 0.0 and 1.0")

    return errors


def parse_spec_json(content: str) -> dict[str, Any]:
    """Parse spec JSON, accepting one optional Markdown code fence."""
    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    parsed = json.loads(stripped)
    if not isinstance(parsed, dict):
        raise ValueError("runtime AC spec JSON must decode to an object")
    return parsed


def _quote_args(parts: Sequence[str]) -> str:
    return " ".join(shlex.quote(str(part)) for part in parts)


def _frontend_command(check: dict[str, Any], runtime_context: dict[str, Any] | None = None) -> str:
    parts = ["python3", "-B", str(ORCH_DIR / "frontend_verify.py"), "--url", check["url"]]
    for assertion in check.get("assertions") or []:
        parts.extend(["--assert", assertion])
    if check.get("click_text"):
        parts.extend(["--click-text", check["click_text"]])
    if check.get("then_text"):
        parts.extend(["--then-text", check["then_text"]])
    if check.get("screenshot"):
        parts.extend(["--screenshot", check["screenshot"]])
    browser_endpoint = check.get("browser_endpoint") or (runtime_context or {}).get(
        "browser_endpoint"
    )
    if browser_endpoint:
        parts.extend(["--browser-endpoint", browser_endpoint])
    return _quote_args(parts)


def _local_verify_command(check: dict[str, Any], runtime_context: dict[str, Any]) -> str:
    worktree = check.get("worktree") or runtime_context.get("worktree") or "."
    base_ref = check.get("base_ref") or runtime_context.get("base_ref") or "HEAD"
    parts = [
        "python3",
        "-B",
        str(ORCH_DIR / "local_verify.py"),
        "--worktree",
        worktree,
        "--base-ref",
        base_ref,
        "--test-cmd",
        check["test_cmd"],
    ]
    for path in check.get("test_paths") or []:
        parts.extend(["--test-path", path])
    return _quote_args(parts)


def _non_regression_command(entry: str | dict[str, Any], idx: int) -> dict[str, Any]:
    if isinstance(entry, str):
        return {"id": f"NR{idx + 1}", "name": f"Non-regression check {idx + 1}", "command": entry}
    return {
        "id": entry.get("id") or f"NR{idx + 1}",
        "name": entry["name"],
        "command": entry["command"],
    }


def build_dry_run_plan(spec: dict[str, Any]) -> dict[str, Any]:
    """Return a dry-run verification plan without executing external commands."""
    errors = validate_spec(spec)
    if errors:
        raise ValueError("invalid runtime AC spec: " + "; ".join(errors))

    meta = spec["verification"]
    runtime_context = spec.get("runtime_context") or {}
    planned_checks: list[dict[str, Any]] = []
    warnings: list[str] = []
    evidence_matrix: dict[str, Any] = {}

    if runtime_context.get("serve_command"):
        server_id = "runtime:start-server"
        planned_checks.append(
            {
                "id": server_id,
                "type": "runtime_context",
                "name": "Start runtime server",
                "command": runtime_context["serve_command"],
                "health_url": runtime_context.get("health_url"),
                "review_before_run": True,
                "mutates_repos": False,
                "manual_stop_required": True,
                "supports_acceptance_criteria": [
                    criterion["id"] for criterion in spec["acceptance_criteria"]
                ],
            }
        )
        warnings.extend(
            _command_review_warnings(
                runtime_context["serve_command"], "runtime_context.serve_command"
            )
        )

    for criterion in spec["acceptance_criteria"]:
        ac_id = criterion["id"]
        provided_evidence: set[str] = set()
        check_ids: list[str] = []
        for check in criterion["checks"]:
            check_type = check["type"]
            check_ids.append(check["id"])
            provided_evidence |= _evidence_for_check(check)
            planned = {
                "id": check["id"],
                "acceptance_criterion": ac_id,
                "type": check_type,
                "name": check["name"],
                "review_before_run": True,
                "mutates_repos": False,
                "evidence": sorted(_evidence_for_check(check)),
            }
            if check_type == "frontend":
                planned["command"] = _frontend_command(check, runtime_context)
                planned["url"] = check["url"]
            elif check_type == "command":
                planned["command"] = check["command"]
                planned["expected"] = check["expected"]
                if "contains" in check:
                    planned["contains"] = check["contains"]
                if "pattern" in check:
                    planned["pattern"] = check["pattern"]
                warnings.extend(
                    _command_review_warnings(check["command"], f"{ac_id}.{check['id']}.command")
                )
            elif check_type == "deliberate_break":
                planned["command"] = _local_verify_command(check, runtime_context)
                planned["test_cmd"] = check["test_cmd"]
                planned["verdicts"] = ["PASS", "FAIL_BROKEN", "FAIL_HOLLOW", "ERROR"]
            elif check_type == "manual":
                planned["instructions"] = check["instructions"]
                planned["command"] = None
            planned_checks.append(planned)

        required = set(criterion["evidence_required"])
        missing_known = sorted((required & VALID_EVIDENCE_TYPES) - provided_evidence)
        evidence_matrix[ac_id] = {
            "statement": criterion["statement"],
            "required": sorted(required),
            "provided": sorted(provided_evidence),
            "missing_known": missing_known,
            "check_ids": check_ids,
        }
        if missing_known:
            warnings.append(f"{ac_id} is missing evidence types {missing_known}")

    non_regression: list[dict[str, Any]] = []
    for idx, entry in enumerate(spec.get("non_regression") or []):
        command_entry = _non_regression_command(entry, idx)
        non_regression.append(
            {
                **command_entry,
                "type": "non_regression",
                "review_before_run": True,
                "mutates_repos": False,
            }
        )
        warnings.extend(
            _command_review_warnings(command_entry["command"], f"non_regression[{idx}].command")
        )

    return {
        "verification_id": meta["id"],
        "title": meta["title"],
        "target": meta.get("target"),
        "repo": meta.get("repo"),
        "goal": meta["goal"],
        "risk_level": meta["risk_level"],
        "runtime_context": {
            "worktree": runtime_context.get("worktree") or ".",
            "serve_command": runtime_context.get("serve_command"),
            "health_url": runtime_context.get("health_url"),
            "base_ref": runtime_context.get("base_ref") or "HEAD",
            "browser_endpoint": runtime_context.get("browser_endpoint"),
            "notes": runtime_context.get("notes") or [],
        },
        "acceptance_criteria": [
            {
                "id": criterion["id"],
                "statement": criterion["statement"],
                "evidence_required": criterion["evidence_required"],
                "check_ids": [check["id"] for check in criterion["checks"]],
            }
            for criterion in spec["acceptance_criteria"]
        ],
        "evidence_matrix": evidence_matrix,
        "planned_checks": planned_checks,
        "non_regression": non_regression,
        "warnings": warnings,
        "verdict_policy": {
            **spec["verdict_policy"],
            "advisory_only": True,
            "verdict_readiness": "needs_review" if warnings else "ready_to_review",
        },
        "execution_policy": {
            "auto_run": False,
            "auto_merge_blocking": False,
            "review_before_run": True,
            "notes": [
                "Dry-run only: no project command is executed by runtime_ac.py.",
                "Run planned checks only after reviewing commands and confirming the app/runtime context.",
                "Use --run with --confirm-run, or --results with external evidence, to produce a gate verdict.",
            ],
        },
    }


def format_plan_text(plan: dict[str, Any], *, emit_commands: bool = False) -> str:
    lines = [
        f"Runtime AC verification plan: {plan['title']} ({plan['verification_id']})",
        f"Target: {plan.get('target') or '(none)'}",
        f"Readiness: {plan['verdict_policy']['verdict_readiness']}",
        f"Acceptance criteria: {len(plan['acceptance_criteria'])}",
        f"Planned checks: {len(plan['planned_checks'])}",
    ]
    if plan["warnings"]:
        lines.append("Warnings:")
        lines.extend(f"- {warning}" for warning in plan["warnings"])
    if emit_commands:
        lines.append("Commands:")
        for check in plan["planned_checks"]:
            if check.get("command"):
                lines.append(f"- [{check['id']}] {check['command']}")
        for check in plan["non_regression"]:
            lines.append(f"- [{check['id']}] {check['command']}")
    return "\n".join(lines)


def _tail(text: str, limit: int = TAIL_CHARS) -> str:
    return text[-limit:] if len(text) > limit else text


def _has_shell_marker(command: str) -> bool:
    return any(marker in command for marker in SHELL_REVIEW_MARKERS)


def _criterion_check_ids(spec: dict[str, Any]) -> dict[str, list[str]]:
    return {
        criterion["id"]: [check["id"] for check in criterion["checks"]]
        for criterion in spec["acceptance_criteria"]
    }


def _all_ac_check_ids(spec: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for ids in _criterion_check_ids(spec).values():
        out.extend(ids)
    return out


def _coerce_result_doc(result_doc: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(result_doc, list):
        return result_doc
    if isinstance(result_doc, dict):
        for key in ("check_results", "results"):
            value = result_doc.get(key)
            if isinstance(value, list):
                return value
    raise ValueError("result JSON must be a list or object with check_results/results")


def evaluate_results(
    spec: dict[str, Any], result_doc: dict[str, Any] | list[dict[str, Any]]
) -> dict[str, Any]:
    """Map check results back to ACs and return PASS/FAIL/NEEDS_REVIEW.

    Result rows must include `{id, status}`. Recognized statuses are PASS, FAIL,
    ERROR, UNSAFE, SKIP, MISSING, NEEDS_REVIEW. Unknown statuses are treated as
    NEEDS_REVIEW so an external collector cannot accidentally pass the gate with
    a novel label.
    """
    errors = validate_spec(spec)
    if errors:
        raise ValueError("invalid runtime AC spec: " + "; ".join(errors))

    results = _coerce_result_doc(result_doc)
    by_id: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []
    for idx, result in enumerate(results):
        if not isinstance(result, dict):
            raise ValueError(f"result[{idx}] must be an object")
        check_id = result.get("id")
        if not _is_nonempty_string(check_id):
            raise ValueError(f"result[{idx}].id must be a non-empty string")
        if check_id in by_id:
            duplicates.append(check_id)
        status = str(result.get("status") or "NEEDS_REVIEW").upper()
        if status not in BLOCKING_STATUSES | INCOMPLETE_STATUSES | {"PASS"}:
            status = "NEEDS_REVIEW"
        by_id[str(check_id)] = {**result, "status": status}
    if duplicates:
        raise ValueError(f"duplicate result ids: {sorted(set(duplicates))}")

    policy = spec["verdict_policy"]
    required_ids = set(policy.get("required_check_ids") or _all_ac_check_ids(spec))
    fail_on_missing = bool(policy.get("fail_on_missing_checks"))
    criterion_summaries: list[dict[str, Any]] = []
    blocking: list[dict[str, Any]] = []
    needs_review: list[dict[str, Any]] = []
    passed_checks = 0
    counted_checks = 0

    for criterion in spec["acceptance_criteria"]:
        ac_id = criterion["id"]
        check_summaries = []
        ac_status = "PASS"
        for check in criterion["checks"]:
            check_id = check["id"]
            required = check_id in required_ids
            result = by_id.get(str(check_id))
            if result is None:
                status = "MISSING"
                result = {"id": check_id, "status": status, "reason": "no result supplied"}
            else:
                status = result["status"]

            counted_checks += 1
            if status == "PASS":
                passed_checks += 1
            if status in BLOCKING_STATUSES or (
                required and status in {"MISSING", "SKIP"} and fail_on_missing
            ):
                ac_status = "FAIL"
                blocking.append(
                    {
                        "ac_id": ac_id,
                        "check_id": check_id,
                        "status": status,
                        "reason": result.get("reason") or result.get("error"),
                    }
                )
            elif status in INCOMPLETE_STATUSES and ac_status != "FAIL":
                ac_status = "NEEDS_REVIEW"
                needs_review.append(
                    {
                        "ac_id": ac_id,
                        "check_id": check_id,
                        "status": status,
                        "reason": result.get("reason") or result.get("error"),
                    }
                )
            check_summaries.append(
                {
                    "id": check_id,
                    "type": check["type"],
                    "required": required,
                    "status": status,
                    "reason": result.get("reason") or result.get("error"),
                }
            )

        criterion_summaries.append(
            {
                "id": ac_id,
                "statement": criterion["statement"],
                "status": ac_status,
                "checks": check_summaries,
            }
        )

    for result in results:
        if str(result.get("type")) == "non_regression":
            status = str(result.get("status") or "NEEDS_REVIEW").upper()
            if status != "PASS":
                blocking.append(
                    {
                        "check_id": result.get("id"),
                        "status": status,
                        "reason": result.get("reason") or result.get("error"),
                    }
                )

    pass_ratio = (passed_checks / counted_checks) if counted_checks else 0.0
    min_ratio = float(policy.get("min_pass_ratio", 1.0))
    if pass_ratio < min_ratio:
        ratio_gap = {
            "status": "NEEDS_REVIEW" if needs_review and not blocking else "FAIL",
            "reason": f"pass ratio {pass_ratio:.3f} is below required {min_ratio:.3f}",
        }
        if ratio_gap["status"] == "NEEDS_REVIEW":
            needs_review.append(ratio_gap)
        else:
            blocking.append(ratio_gap)

    if blocking:
        verdict = "FAIL"
    elif needs_review:
        verdict = "NEEDS_REVIEW"
    else:
        verdict = "PASS"

    return {
        "verification_id": spec["verification"]["id"],
        "target": spec["verification"].get("target"),
        "verdict": verdict,
        "verifier_verdict": VERDICT_BY_GATE[verdict],
        "pass_ratio": round(pass_ratio, 4),
        "required_check_ids": sorted(required_ids),
        "criteria": criterion_summaries,
        "blocking": blocking,
        "needs_review": needs_review,
        "result_count": len(results),
    }


def record_gate_verdict(run_id: str, gate: dict[str, Any]) -> dict[str, Any]:
    verifier_verdict = gate.get("verifier_verdict") or VERDICT_BY_GATE.get(
        gate.get("verdict") or "NEEDS_REVIEW_RUNTIME_AC", "NEEDS_REVIEW_RUNTIME_AC"
    )
    feedback.record_outcome(
        run_id,
        verifier_verdict=verifier_verdict,
        notes=f"runtime_ac: {verifier_verdict} - {gate.get('verdict')} for {gate.get('verification_id')}",
    )
    feedback.record_completion_event(
        run_id,
        event_type="verification",
        phase="verification",
        producer="runtime_ac",
        status=gate.get("verdict"),
        payload={
            "acceptance_gate_ids": [gate.get("verification_id") or "runtime-ac"],
            "test_ids": gate.get("required_check_ids") or [],
            "result_hashes": [feedback._completion_hash(gate)],
            "verification": {
                "verifier_verdict": verifier_verdict,
                "verifier_ids": ["runtime_ac"],
                "result_hashes": {"gate": feedback._completion_hash(gate)},
            },
        },
    )
    return {"run_id": run_id, "verifier_verdict": verifier_verdict, "recorded": True}


def _completed_result(
    check: dict[str, Any], completed: subprocess.CompletedProcess[str], duration_s: float
) -> dict[str, Any]:
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    combined = stdout + "\n" + stderr
    status = "PASS" if completed.returncode == 0 else "FAIL"
    reason = None

    if check.get("type") == "frontend":
        try:
            parsed = json.loads(stdout or "{}")
        except Exception:
            parsed = None
        status = "PASS" if isinstance(parsed, dict) and parsed.get("ok") else "FAIL"
        reason = (
            None
            if status == "PASS"
            else (
                (parsed or {}).get("error")
                if isinstance(parsed, dict)
                else "frontend verifier output was not JSON"
            )
        )
    elif check.get("type") == "deliberate_break":
        try:
            parsed = json.loads(stdout or "{}")
        except Exception:
            parsed = None
        verdict = parsed.get("verdict") if isinstance(parsed, dict) else None
        status = "PASS" if verdict == "PASS" else "FAIL"
        reason = (
            None
            if status == "PASS"
            else (parsed or {}).get("reason")
            or (parsed or {}).get("error")
            or f"local_verify verdict {verdict!r}"
        )
    elif check.get("type") == "command":
        expected = check.get("expected") or "exit_0"
        if expected == "exit_0":
            status = "PASS" if completed.returncode == 0 else "FAIL"
        elif expected == "contains":
            needle = check.get("contains") or ""
            if completed.returncode != 0:
                status = "FAIL"
                reason = f"command exited {completed.returncode}"
            else:
                status = "PASS" if needle in combined else "FAIL"
                reason = None if status == "PASS" else f"expected output to contain {needle!r}"
        elif expected == "regex":
            pattern = check.get("pattern") or ""
            if completed.returncode != 0:
                status = "FAIL"
                reason = f"command exited {completed.returncode}"
            else:
                status = "PASS" if re.search(pattern, combined) else "FAIL"
                reason = None if status == "PASS" else f"expected output to match {pattern!r}"
        elif expected == "manual_review":
            status = "NEEDS_REVIEW" if completed.returncode == 0 else "FAIL"
            reason = "command output requires manual review" if completed.returncode == 0 else None
    elif check.get("type") == "non_regression":
        status = "PASS" if completed.returncode == 0 else "FAIL"

    if reason is None and status == "FAIL":
        reason = f"command exited {completed.returncode}"
    return {
        "id": check["id"],
        "type": check.get("type"),
        "status": status,
        "reason": reason,
        "command": check.get("command"),
        "returncode": completed.returncode,
        "duration_s": round(duration_s, 3),
        "stdout_tail": _tail(stdout),
        "stderr_tail": _tail(stderr),
    }


def _run_command_check(check: dict[str, Any], *, cwd: Path, timeout: int) -> dict[str, Any]:
    started = time.time()
    try:
        completed = subprocess.run(
            shlex.split(check["command"]),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "id": check["id"],
            "type": check.get("type"),
            "status": "ERROR",
            "reason": f"timed out after {timeout}s",
            "command": check.get("command"),
            "duration_s": round(time.time() - started, 3),
            "stdout_tail": _tail((exc.stdout or "") if isinstance(exc.stdout, str) else ""),
            "stderr_tail": _tail((exc.stderr or "") if isinstance(exc.stderr, str) else ""),
        }
    except Exception as exc:
        return {
            "id": check["id"],
            "type": check.get("type"),
            "status": "ERROR",
            "reason": str(exc),
            "command": check.get("command"),
            "duration_s": round(time.time() - started, 3),
            "stdout_tail": "",
            "stderr_tail": "",
        }
    return _completed_result(check, completed, time.time() - started)


def _planned_checks_for_run(
    plan: dict[str, Any], check_ids: set[str] | None
) -> list[dict[str, Any]]:
    checks = [check for check in plan["planned_checks"] if check.get("type") != "runtime_context"]
    checks.extend(plan.get("non_regression") or [])
    if check_ids is None:
        return checks
    return [check for check in checks if check.get("id") in check_ids]


def run_verification(
    spec: dict[str, Any],
    *,
    confirm_run: bool = False,
    allow_command_checks: bool = False,
    timeout: int = 120,
    check_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Run selected checks and return check results plus the gate verdict."""
    if not confirm_run:
        raise ValueError("--confirm-run is required before runtime_ac.py executes any check")

    plan = build_dry_run_plan(spec)
    cwd = Path(plan["runtime_context"]["worktree"]).resolve()
    selected = set(check_ids) if check_ids else None
    results: list[dict[str, Any]] = []
    for check in _planned_checks_for_run(plan, selected):
        check_type = check.get("type")
        if check_type == "manual":
            results.append(
                {
                    "id": check["id"],
                    "type": check_type,
                    "status": "NEEDS_REVIEW",
                    "reason": "manual evidence must be supplied externally",
                }
            )
            continue
        # See ORCH-ANCHOR: runtime-ac-command-exec-gate for which types this holds, and why
        # deliberate_break is not one of them.
        if command_execution_gated(check_type) and not allow_command_checks:
            results.append(
                {
                    "id": check["id"],
                    "type": check_type,
                    "status": "SKIP",
                    "reason": "--allow-command-checks is required for command/non_regression checks",
                    "command": check.get("command"),
                }
            )
            continue
        command = check.get("command")
        if not _is_nonempty_string(command):
            results.append(
                {
                    "id": check["id"],
                    "type": check_type,
                    "status": "ERROR",
                    "reason": "no command to run",
                }
            )
            continue
        shell_candidate = ""
        if check_type in {"command", "non_regression"}:
            shell_candidate = str(command or "")
        elif check_type == "deliberate_break":
            shell_candidate = check.get("test_cmd") or ""
        if shell_candidate and _has_shell_marker(shell_candidate):
            results.append(
                {
                    "id": check["id"],
                    "type": check_type,
                    "status": "UNSAFE",
                    "reason": "command contains shell control markers; shell execution is not supported by this runner",
                    "command": command,
                }
            )
            continue
        results.append(_run_command_check(check, cwd=cwd, timeout=timeout))

    gate = evaluate_results(spec, {"check_results": results})
    return {
        "verification_id": plan["verification_id"],
        "target": plan.get("target"),
        "generated_at": int(time.time()),
        "confirmed_run": True,
        "allow_command_checks": allow_command_checks,
        "timeout": timeout,
        "check_results": results,
        "gate": gate,
    }


def _valid_spec() -> dict[str, Any]:
    return json.loads(json.dumps(RUNTIME_AC_SCHEMA_EXAMPLE))


def _selftest() -> None:
    prompt = build_authoring_prompt(
        goal="Verify course progress end to end",
        repo="owner/repo",
        target="owner/repo#123",
        context="Use the local frontend verifier where possible.",
    )
    assert "runtime acceptance-criteria verification lane" in prompt, prompt
    assert '"acceptance_criteria"' in prompt and "frontend_verify.py" in prompt, prompt

    valid = _valid_spec()
    assert validate_spec(valid) == []
    plan = build_dry_run_plan(valid)
    assert plan["verification_id"] == "course-progress-runtime-ac", plan
    assert plan["execution_policy"]["auto_run"] is False, plan
    assert plan["verdict_policy"]["advisory_only"] is True, plan
    assert plan["evidence_matrix"]["AC1"]["missing_known"] == [], plan["evidence_matrix"]
    commands = [check.get("command") or "" for check in plan["planned_checks"]]
    assert any(
        "frontend_verify.py" in command and "--assert text:Progress" in command
        for command in commands
    ), commands
    assert any(
        "local_verify.py" in command and "--test-path tests/test_course_progress.py" in command
        for command in commands
    ), commands
    assert plan["non_regression"] and plan["non_regression"][0]["review_before_run"] is True, plan[
        "non_regression"
    ]
    endpoint_spec = _valid_spec()
    endpoint_spec["runtime_context"]["browser_endpoint"] = "http://127.0.0.1:9222"
    endpoint_plan = build_dry_run_plan(endpoint_spec)
    endpoint_commands = [check.get("command") or "" for check in endpoint_plan["planned_checks"]]
    assert any(
        "--browser-endpoint http://127.0.0.1:9222" in command for command in endpoint_commands
    ), endpoint_commands

    duplicate_ac = _valid_spec()
    duplicate_ac["acceptance_criteria"][1]["id"] = "AC1"
    assert any("acceptance_criteria[1].id duplicates" in err for err in validate_spec(duplicate_ac))

    duplicate_check = _valid_spec()
    duplicate_check["acceptance_criteria"][1]["checks"][0]["id"] = "AC1-FE"
    assert any("duplicates 'AC1-FE'" in err for err in validate_spec(duplicate_check))

    bad_assertion = _valid_spec()
    bad_assertion["acceptance_criteria"][0]["checks"][0]["assertions"] = ["css:.progress"]
    assert any("must start with 'text:' or 'role:'" in err for err in validate_spec(bad_assertion))

    unsafe = _valid_spec()
    unsafe["acceptance_criteria"][0]["checks"][1]["command"] = "git reset --hard"
    assert any("mutating git subcommand" in err for err in validate_spec(unsafe))

    no_break = _valid_spec()
    no_break["acceptance_criteria"][0]["checks"] = no_break["acceptance_criteria"][0]["checks"][:2]
    no_break["acceptance_criteria"][0]["evidence_required"] = ["runtime_behavior", "test_output"]
    assert any("require_deliberate_break_for_tests" in err for err in validate_spec(no_break))

    parsed = parse_spec_json('```json\n{"verification": {"id": "x"}}\n```')
    assert parsed == {"verification": {"id": "x"}}, parsed

    text = format_plan_text(plan, emit_commands=True)
    assert "Runtime AC verification plan" in text and "Commands:" in text, text

    command_spec = {
        "verification": {
            "id": "command-runtime-ac",
            "title": "Command runtime AC",
            "goal": "Verify a command-backed AC",
            "risk_level": "low",
        },
        "runtime_context": {"worktree": str(ORCH_DIR)},
        "acceptance_criteria": [
            {
                "id": "AC1",
                "statement": "The check prints ok.",
                "evidence_required": ["command_output"],
                "checks": [
                    {
                        "id": "AC1-CMD",
                        "type": "command",
                        "name": "Print ok",
                        "command": f"{shlex.quote(sys.executable)} -c 'print(\"ok\")'",
                        "expected": "contains",
                        "contains": "ok",
                    }
                ],
            }
        ],
        "verdict_policy": {
            "require_runtime_evidence": False,
            "require_deliberate_break_for_tests": False,
            "fail_on_missing_checks": True,
            "required_check_ids": ["AC1-CMD"],
            "min_pass_ratio": 1.0,
        },
    }
    gate_pass = evaluate_results(
        command_spec, {"check_results": [{"id": "AC1-CMD", "status": "PASS"}]}
    )
    assert (
        gate_pass["verdict"] == "PASS" and gate_pass["verifier_verdict"] == "PASS_RUNTIME_AC"
    ), gate_pass
    gate_fail = evaluate_results(
        command_spec, {"check_results": [{"id": "AC1-CMD", "status": "FAIL"}]}
    )
    assert gate_fail["verdict"] == "FAIL" and gate_fail["blocking"], gate_fail
    try:
        run_verification(command_spec, confirm_run=False, allow_command_checks=True)
        raise AssertionError("run_verification must require confirm_run")
    except ValueError as exc:
        assert "--confirm-run" in str(exc), exc
    skipped = run_verification(command_spec, confirm_run=True, allow_command_checks=False)
    assert skipped["check_results"][0]["status"] == "SKIP", skipped
    assert skipped["gate"]["verdict"] == "FAIL", skipped["gate"]

    # THE SPLIT (ORCH-ANCHOR: runtime-ac-command-exec-gate). ORCH_RUNTIME_AC_ALLOW_COMMANDS decides
    # agent-authored command strings and NOTHING else. A stored switch criterion claimed it also
    # held the template-built deliberate_break check and asked for the flag to be split before it
    # could be flipped; the split was already here, and that stale verdict is what parked
    # `deliberate-break-verifier` in the held-switch bucket.
    assert command_execution_gated("command"), "agent-authored commands must stay opt-in"
    assert command_execution_gated("non_regression"), "agent-authored commands must stay opt-in"
    assert not command_execution_gated(
        "deliberate_break"
    ), "deliberate_break is template-built; gating it here is what made the switch look unflippable"
    assert not command_execution_gated("frontend") and not command_execution_gated("manual")
    assert not command_execution_gated(None)
    # And prove it through the RUNTIME path, not just the predicate: with the flag OFF a
    # deliberate_break check must reach the shell-marker screen (UNSAFE), never be SKIPped. A
    # regression that adds deliberate_break to COMMAND_EXEC_GATED_TYPES turns this into SKIP.
    marker_break = {
        "verification": {"id": "break-not-gated", "title": "t", "goal": "g", "risk_level": "low"},
        "runtime_context": {"worktree": str(ORCH_DIR)},
        "acceptance_criteria": [
            {
                "id": "AC1",
                "statement": "s",
                "evidence_required": ["test_output", "deliberate_break"],
                "checks": [
                    {
                        "id": "AC1-BREAK",
                        "type": "deliberate_break",
                        "name": "n",
                        "test_cmd": "pytest x && echo y",
                        "test_paths": ["t.py"],
                        "target_paths": ["m.py"],
                    }
                ],
            }
        ],
        "verdict_policy": {
            "require_runtime_evidence": False,
            "require_deliberate_break_for_tests": True,
            "fail_on_missing_checks": True,
            "required_check_ids": ["AC1-BREAK"],
            "min_pass_ratio": 1.0,
        },
    }
    not_gated = run_verification(marker_break, confirm_run=True, allow_command_checks=False)
    assert not_gated["check_results"][0]["status"] == "UNSAFE", not_gated["check_results"]
    executed = run_verification(
        command_spec, confirm_run=True, allow_command_checks=True, timeout=30
    )
    assert executed["check_results"][0]["status"] == "PASS", executed
    assert executed["gate"]["verdict"] == "PASS", executed["gate"]

    nonzero_contains = json.loads(json.dumps(command_spec))
    nonzero_contains["verification"]["id"] = "nonzero-contains-runtime-ac"
    nonzero_contains["acceptance_criteria"][0]["checks"][0][
        "command"
    ] = f"{shlex.quote(sys.executable)} -c 'raise SystemExit(\"ok\")'"
    nonzero_run = run_verification(
        nonzero_contains, confirm_run=True, allow_command_checks=True, timeout=30
    )
    assert nonzero_run["check_results"][0]["status"] == "FAIL", nonzero_run
    assert nonzero_run["gate"]["verdict"] == "FAIL", nonzero_run["gate"]

    timeout_spec = json.loads(json.dumps(command_spec))
    timeout_spec["verification"]["id"] = "timeout-runtime-ac"
    timeout_spec["acceptance_criteria"][0]["checks"][0][
        "command"
    ] = f"{shlex.quote(sys.executable)} -c '__import__(\"time\").sleep(2)'"
    timeout_run = run_verification(
        timeout_spec, confirm_run=True, allow_command_checks=True, timeout=1
    )
    assert timeout_run["check_results"][0]["status"] == "ERROR", timeout_run
    assert "timed out" in timeout_run["check_results"][0]["reason"], timeout_run

    expansion_spec = json.loads(json.dumps(command_spec))
    expansion_spec["verification"]["id"] = "expansion-warning-runtime-ac"
    expansion_spec["acceptance_criteria"][0]["checks"][0]["command"] = "printf $HOME"
    expansion_plan = build_dry_run_plan(expansion_spec)
    assert any(
        "shell expansion marker" in warning for warning in expansion_plan["warnings"]
    ), expansion_plan

    manual_spec = json.loads(json.dumps(command_spec))
    manual_spec["verification"]["id"] = "manual-runtime-ac"
    manual_spec["acceptance_criteria"][0]["evidence_required"] = ["manual_review"]
    manual_spec["acceptance_criteria"][0]["checks"] = [
        {
            "id": "AC1-MANUAL",
            "type": "manual",
            "name": "Manual evidence",
            "instructions": "Inspect the staged environment.",
        }
    ]
    manual_spec["verdict_policy"]["required_check_ids"] = ["AC1-MANUAL"]
    manual_gate = run_verification(manual_spec, confirm_run=True)
    assert manual_gate["gate"]["verdict"] == "NEEDS_REVIEW", manual_gate

    print(
        "runtime_ac.py selftest: OK (authoring prompt, AC-bound schema validation, dry-run plan, "
        "command emission, opt-in execution, result gate)"
    )


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Build or validate an Orchestrator runtime AC verification spec."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--goal", help="goal/issue to turn into an authoring prompt")
    group.add_argument("--goal-file", help="file containing the goal/issue")
    group.add_argument("--validate", help="runtime AC JSON file to validate")
    group.add_argument("--plan", help="runtime AC JSON file to turn into a dry-run plan")
    group.add_argument("--run", help="runtime AC JSON file to execute with --confirm-run")
    group.add_argument("--results", help="runtime AC JSON file to evaluate against --result-json")
    group.add_argument("--selftest", action="store_true", help="run offline selftests")
    parser.add_argument("--repo", help="optional owner/repo context")
    parser.add_argument("--target", help="optional issue/PR target context")
    parser.add_argument(
        "--context", default="", help="inline context to include in the authoring prompt"
    )
    parser.add_argument(
        "--context-file", help="extra context file to include in the authoring prompt"
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    parser.add_argument(
        "--emit-commands", action="store_true", help="include planned commands in text output"
    )
    parser.add_argument(
        "--confirm-run", action="store_true", help="required before --run executes any checks"
    )
    parser.add_argument(
        "--allow-command-checks",
        action="store_true",
        help="allow --run to execute command and non_regression checks",
    )
    parser.add_argument("--timeout", type=int, default=120, help="per-check timeout for --run")
    parser.add_argument(
        "--check-id", action="append", default=[], help="run only this check id; repeatable"
    )
    parser.add_argument("--result-json", help="result JSON file for --results")
    parser.add_argument(
        "--record-run-id", help="optional feedback.run_id to patch with runtime AC verifier verdict"
    )
    args = parser.parse_args(list(argv))

    if args.selftest:
        _selftest()
        return 0

    if args.validate:
        try:
            spec = parse_spec_json(Path(args.validate).read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"Could not read or parse runtime AC JSON: {exc}", file=sys.stderr)
            return 2
        errors = validate_spec(spec)
        if args.json:
            print(json.dumps({"valid": not errors, "errors": errors}, indent=2))
        elif errors:
            print("Validation FAILED:", file=sys.stderr)
            for error in errors:
                print(f"- {error}", file=sys.stderr)
        else:
            print("Validation PASSED")
        return 0 if not errors else 1

    if args.plan:
        try:
            spec = parse_spec_json(Path(args.plan).read_text(encoding="utf-8"))
            plan = build_dry_run_plan(spec)
        except Exception as exc:
            print(f"Could not build runtime AC plan: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(plan, indent=2))
        else:
            print(format_plan_text(plan, emit_commands=args.emit_commands))
        return 0

    if args.run:
        try:
            spec = parse_spec_json(Path(args.run).read_text(encoding="utf-8"))
            run = run_verification(
                spec,
                confirm_run=args.confirm_run,
                allow_command_checks=args.allow_command_checks,
                timeout=args.timeout,
                check_ids=args.check_id or None,
            )
            if args.record_run_id:
                run["feedback"] = record_gate_verdict(args.record_run_id, run["gate"])
        except Exception as exc:
            print(f"Could not run runtime AC checks: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(run, indent=2))
        return 0 if run["gate"]["verdict"] == "PASS" else 1

    if args.results:
        if not args.result_json:
            print("--results requires --result-json", file=sys.stderr)
            return 2
        try:
            spec = parse_spec_json(Path(args.results).read_text(encoding="utf-8"))
            result_doc = json.loads(Path(args.result_json).read_text(encoding="utf-8"))
            gate = evaluate_results(spec, result_doc)
            if args.record_run_id:
                gate["feedback"] = record_gate_verdict(args.record_run_id, gate)
        except Exception as exc:
            print(f"Could not evaluate runtime AC results: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(gate, indent=2))
        return 0 if gate["verdict"] == "PASS" else 1

    goal = args.goal or Path(args.goal_file).read_text(encoding="utf-8")
    prompt = build_authoring_prompt(
        goal=goal,
        repo=args.repo,
        target=args.target,
        context=args.context,
        context_file=args.context_file,
    )
    if args.json:
        print(json.dumps({"prompt": prompt}, indent=2))
    else:
        print(prompt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
