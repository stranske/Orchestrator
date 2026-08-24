#!/usr/bin/env python3
"""Build and validate Orchestrator codemod/refactor campaign plans.

The codemod lane is the first increment for cross-file structural campaigns:
author a strict campaign JSON, validate it conservatively, and emit a dry-run
plan with review-before-run commands. It does not execute external tools or
open batched PRs.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

VALID_TOOLS = {"ast-grep", "comby", "jscodeshift", "openrewrite", "custom"}
VALID_RISK_LEVELS = {"low", "medium", "high"}
VALID_PR_STRATEGIES = {"single", "batched", "per_repo", "draft_only"}
MUTATING_FLAGS = {"--apply", "--write", "--in-place", "--fix", "-i"}

CAMPAIGN_SCHEMA_EXAMPLE = {
    "campaign": {
        "id": "rename-legacy-handler",
        "title": "Rename LegacyHandler to ModernHandler",
        "goal": "Apply a consistent rename across Python modules",
        "repos": ["owner/repo"],
        "constraints": ["Do not change public API docs until code lands"],
        "definition_of_done": ["All targeted files use ModernHandler", "Tests pass"],
    },
    "scope": {
        "include_globs": ["**/*.py"],
        "exclude_globs": ["**/migrations/**", "**/vendor/**"],
        "max_files": 200,
    },
    "recipe": {
        "tool": "ast-grep",
        "language": "python",
        "summary": "Rename class LegacyHandler to ModernHandler",
        "match": "LegacyHandler",
        "rewrite": "ModernHandler",
        "risk_level": "medium",
    },
    "rollout": {
        "batch_size": 25,
        "branch_prefix": "codemod/rename-legacy-handler",
        "pr_strategy": "batched",
    },
    "acceptance": ["No remaining LegacyHandler references in scoped files"],
    "validation": ["pytest", "ruff check"],
    "manual_review": ["Inspect non-mechanical import re-exports before merge"],
    "delegate_prompt": "Apply the validated campaign and report non-mechanical follow-ups.",
}


def _read_optional(path: str | Path | None) -> str:
    if not path:
        return ""
    return Path(path).read_text(encoding="utf-8")


def build_authoring_prompt(
    *,
    goal: str,
    repos: Sequence[str] | None = None,
    target: str | None = None,
    context: str = "",
    context_file: str | Path | None = None,
) -> str:
    """Return the prompt for an offloaded campaign-authoring pass."""
    if not goal.strip():
        raise ValueError("goal must be non-empty")

    file_context = _read_optional(context_file)
    context_parts = [part.strip() for part in (context, file_context) if part and part.strip()]
    context_block = (
        "\n\nAdditional context:\n" + "\n\n".join(context_parts) if context_parts else ""
    )
    repos_block = "\nRepositories: " + ", ".join(repos) if repos else ""
    target_block = f"\nTarget: {target}" if target else ""
    schema = json.dumps(CAMPAIGN_SCHEMA_EXAMPLE, indent=2)

    return f"""You are in the Orchestrator codemod/refactor-campaign lane.

Goal:
{goal.strip()}{repos_block}{target_block}{context_block}

Author a conservative cross-file structural change campaign. Prefer deterministic
tools such as ast-grep, Comby, jscodeshift, or OpenRewrite over ad-hoc regex.
Reserve manual_review for cases that cannot be expressed safely as a mechanical
recipe.

Return exactly one JSON object and no prose. The object must match this shape:

{schema}

Rules:
- campaign.id must be a stable slug: lowercase letters, numbers, and hyphens.
- campaign.repos must be a non-empty list of owner/repo strings when known.
- scope.include_globs must be non-empty; keep scope as narrow as the goal allows.
- scope.max_files must be a positive integer cap for first-rollout review.
- recipe.tool must be one of: ast-grep, comby, jscodeshift, openrewrite, custom.
- recipe must include exactly one recipe mechanism:
  (a) non-empty match AND rewrite strings, OR
  (b) a non-empty rule_file path, OR
  (c) a non-empty command_template string.
- recipe.risk_level must be low, medium, or high.
- rollout.batch_size must be >= 1; branch_prefix must be non-empty.
- rollout.pr_strategy must be one of: single, batched, per_repo, draft_only.
- acceptance, validation, and manual_review must be non-empty string lists.
- delegate_prompt must tell a delegated agent how to execute/review the campaign safely.
- Do not include mutating shell commands; the orchestrator derives dry-run commands separately.
"""


def _is_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


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


def _validate_positive_int(value: Any, path: str) -> list[str]:
    if not isinstance(value, int) or isinstance(value, bool):
        return [f"{path} must be an integer"]
    if value < 1:
        return [f"{path} must be >= 1"]
    return []


def _looks_like_slug(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]*", value))


def validate_campaign(campaign: dict[str, Any]) -> list[str]:
    """Validate an agent-produced codemod campaign JSON."""
    errors: list[str] = []
    if not isinstance(campaign, dict):
        return ["campaign document must be a JSON object"]

    required_top = (
        "campaign",
        "scope",
        "recipe",
        "rollout",
        "acceptance",
        "validation",
        "manual_review",
        "delegate_prompt",
    )
    errors.extend(_validate_required_mapping(campaign, "plan", required_top))
    if errors:
        return errors

    meta = campaign["campaign"]
    errors.extend(
        _validate_required_mapping(
            meta,
            "campaign",
            ("id", "title", "goal", "repos", "constraints", "definition_of_done"),
        )
    )
    if isinstance(meta, dict):
        campaign_id = meta.get("id")
        if not _is_nonempty_string(campaign_id):
            errors.append("campaign.id must be a non-empty string")
        elif not _looks_like_slug(campaign_id.strip()):
            errors.append("campaign.id must be a lowercase slug")
        if not _is_nonempty_string(meta.get("title")):
            errors.append("campaign.title must be a non-empty string")
        if not _is_nonempty_string(meta.get("goal")):
            errors.append("campaign.goal must be a non-empty string")
        errors.extend(_validate_string_list(meta.get("repos"), "campaign.repos", nonempty=True))
        errors.extend(
            _validate_string_list(meta.get("constraints"), "campaign.constraints", nonempty=False)
        )
        errors.extend(
            _validate_string_list(
                meta.get("definition_of_done"), "campaign.definition_of_done", nonempty=True
            )
        )

    scope = campaign["scope"]
    errors.extend(
        _validate_required_mapping(scope, "scope", ("include_globs", "exclude_globs", "max_files"))
    )
    if isinstance(scope, dict):
        errors.extend(
            _validate_string_list(scope.get("include_globs"), "scope.include_globs", nonempty=True)
        )
        errors.extend(
            _validate_string_list(scope.get("exclude_globs"), "scope.exclude_globs", nonempty=False)
        )
        errors.extend(_validate_positive_int(scope.get("max_files"), "scope.max_files"))

    recipe = campaign["recipe"]
    errors.extend(
        _validate_required_mapping(recipe, "recipe", ("tool", "language", "summary", "risk_level"))
    )
    if isinstance(recipe, dict):
        if recipe.get("tool") not in VALID_TOOLS:
            errors.append(f"recipe.tool must be one of {sorted(VALID_TOOLS)}")
        if not _is_nonempty_string(recipe.get("language")):
            errors.append("recipe.language must be a non-empty string")
        if not _is_nonempty_string(recipe.get("summary")):
            errors.append("recipe.summary must be a non-empty string")
        if recipe.get("risk_level") not in VALID_RISK_LEVELS:
            errors.append(f"recipe.risk_level must be one of {sorted(VALID_RISK_LEVELS)}")

        match = recipe.get("match")
        rewrite = recipe.get("rewrite")
        rule_file = recipe.get("rule_file")
        command_template = recipe.get("command_template")
        has_match_rewrite = _is_nonempty_string(match) and _is_nonempty_string(rewrite)
        has_rule_file = _is_nonempty_string(rule_file)
        has_command_template = _is_nonempty_string(command_template)
        mechanism_count = sum([has_match_rewrite, has_rule_file, has_command_template])
        if mechanism_count != 1:
            errors.append(
                "recipe must include exactly one mechanism: (match+rewrite) OR rule_file OR command_template"
            )
        if match is not None and not isinstance(match, str):
            errors.append("recipe.match must be a string when present")
        if rewrite is not None and not isinstance(rewrite, str):
            errors.append("recipe.rewrite must be a string when present")
        if rule_file is not None and not isinstance(rule_file, str):
            errors.append("recipe.rule_file must be a string when present")
        if command_template is not None and not isinstance(command_template, str):
            errors.append("recipe.command_template must be a string when present")

    rollout = campaign["rollout"]
    errors.extend(
        _validate_required_mapping(
            rollout, "rollout", ("batch_size", "branch_prefix", "pr_strategy")
        )
    )
    if isinstance(rollout, dict):
        errors.extend(_validate_positive_int(rollout.get("batch_size"), "rollout.batch_size"))
        if not _is_nonempty_string(rollout.get("branch_prefix")):
            errors.append("rollout.branch_prefix must be a non-empty string")
        if rollout.get("pr_strategy") not in VALID_PR_STRATEGIES:
            errors.append(f"rollout.pr_strategy must be one of {sorted(VALID_PR_STRATEGIES)}")

    errors.extend(_validate_string_list(campaign.get("acceptance"), "acceptance", nonempty=True))
    errors.extend(_validate_string_list(campaign.get("validation"), "validation", nonempty=True))
    errors.extend(
        _validate_string_list(campaign.get("manual_review"), "manual_review", nonempty=True)
    )
    if not _is_nonempty_string(campaign.get("delegate_prompt")):
        errors.append("delegate_prompt must be a non-empty string")
    return errors


def parse_campaign_json(content: str) -> dict[str, Any]:
    """Parse campaign JSON, accepting a single optional Markdown code fence."""
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
        raise ValueError("campaign JSON must decode to an object")
    return parsed


def _rg_scope_command(
    include_globs: Sequence[str], exclude_globs: Sequence[str], max_files: int
) -> str:
    parts = ["rg", "--files"]
    for glob in include_globs:
        parts.extend(["-g", shlex.quote(glob)])
    for glob in exclude_globs:
        excluded = glob if glob.startswith("!") else f"!{glob}"
        parts.extend(["-g", shlex.quote(excluded)])
    return " ".join(parts + ["|", "head", "-n", str(max_files)])


def _template_is_safe_dry_run(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    lowered_tokens = {token.lower() for token in tokens}
    if lowered_tokens & MUTATING_FLAGS:
        return False
    lowered = command.lower()
    if " rewrite:run" in lowered and "dryrun" not in lowered and "dry-run" not in lowered:
        return False
    return True


def _has_dry_run_marker(command: str) -> bool:
    lowered = command.lower()
    return any(
        marker in lowered for marker in ("dryrun", "dry-run", "--dry", " -dry", "-diff", "--print")
    )


def _recipe_dry_run_command(
    recipe: dict[str, Any], scope: dict[str, Any]
) -> tuple[str | None, str | None]:
    """Build a conservative dry-run command when enough recipe fields exist."""
    tool = recipe.get("tool")
    include = scope.get("include_globs") or ["**/*"]
    language = recipe.get("language", "")
    match = recipe.get("match")
    rewrite = recipe.get("rewrite")
    rule_file = recipe.get("rule_file")
    command_template = recipe.get("command_template")

    if tool == "ast-grep":
        if _is_nonempty_string(rule_file):
            return " ".join(["ast-grep", "scan", "--rule", shlex.quote(rule_file.strip())]), None
        if _is_nonempty_string(match):
            return (
                " ".join(
                    [
                        "ast-grep",
                        "scan",
                        "-l",
                        shlex.quote(language),
                        "-p",
                        shlex.quote(match.strip()),
                    ]
                ),
                None,
            )

    if tool == "comby" and _is_nonempty_string(match) and _is_nonempty_string(rewrite):
        target = include[0] if include else "."
        matcher = ".py" if language == "python" else language
        return (
            " ".join(
                [
                    "comby",
                    shlex.quote(match.strip()),
                    shlex.quote(rewrite.strip()),
                    shlex.quote(target),
                    "-matcher",
                    shlex.quote(matcher),
                    "-diff",
                ]
            ),
            None,
        )

    if tool == "jscodeshift":
        if _is_nonempty_string(rule_file):
            target = include[0] if include else "."
            return (
                " ".join(
                    [
                        "jscodeshift",
                        "-t",
                        shlex.quote(rule_file.strip()),
                        "--dry",
                        "--print",
                        shlex.quote(target),
                    ]
                ),
                None,
            )
        if _is_nonempty_string(command_template):
            cmd = command_template.strip()
            if _template_is_safe_dry_run(cmd) and _has_dry_run_marker(cmd):
                return cmd, None
            return None, "jscodeshift command_template omitted because it is not clearly dry-run"

    if tool == "openrewrite":
        if _is_nonempty_string(command_template):
            cmd = command_template.strip()
            if _template_is_safe_dry_run(cmd) and _has_dry_run_marker(cmd):
                return cmd, None
            return (
                None,
                "openrewrite command_template omitted because it lacks explicit dry-run semantics",
            )
        if _is_nonempty_string(rule_file):
            return (
                " ".join(
                    [
                        "./mvnw",
                        f"-Drewrite.activeRecipes={shlex.quote(rule_file.strip())}",
                        "rewrite:dryRun",
                    ]
                ),
                None,
            )

    if tool == "custom" and _is_nonempty_string(command_template):
        cmd = command_template.strip()
        if _template_is_safe_dry_run(cmd) and _has_dry_run_marker(cmd):
            return cmd, None
        return None, "custom command_template omitted because it is not clearly safe dry-run"

    return None, f"no dry-run command could be derived for tool {tool!r}"


def build_dry_run_plan(campaign: dict[str, Any]) -> dict[str, Any]:
    """Return a dry-run rollout plan without executing external tools."""
    errors = validate_campaign(campaign)
    if errors:
        raise ValueError("invalid campaign: " + "; ".join(errors))

    meta = campaign["campaign"]
    scope = campaign["scope"]
    recipe = campaign["recipe"]
    rollout = campaign["rollout"]
    include = scope["include_globs"]
    exclude = scope.get("exclude_globs") or []
    max_files = scope["max_files"]
    batch_size = min(rollout["batch_size"], max_files)

    commands: list[dict[str, Any]] = []
    warnings: list[str] = []
    recipe_cmd, recipe_warning = _recipe_dry_run_command(recipe, scope)
    if recipe_cmd:
        commands.append(
            {
                "purpose": "recipe_dry_run",
                "tool": recipe["tool"],
                "command": recipe_cmd,
                "review_before_run": True,
                "mutates_files": False,
            }
        )
    if recipe_warning:
        warnings.append(recipe_warning)

    commands.append(
        {
            "purpose": "scope_inventory",
            "command": _rg_scope_command(include, exclude, max_files),
            "review_before_run": True,
            "mutates_files": False,
        }
    )
    for repo in meta["repos"]:
        commands.append(
            {
                "purpose": "proposed_branch",
                "repo": repo,
                "branch": f"{rollout['branch_prefix']}/{meta['id']}",
                "review_before_run": True,
                "mutates_files": False,
                "note": "Branch creation is planned only; not executed in this increment.",
            }
        )
    for cmd in campaign["validation"]:
        commands.append(
            {
                "purpose": "validation",
                "command": cmd,
                "review_before_run": True,
                "mutates_files": False,
            }
        )

    return {
        "campaign_id": meta["id"],
        "title": meta["title"],
        "goal": meta["goal"],
        "repos": meta["repos"],
        "scope_summary": {
            "include_globs": include,
            "exclude_globs": exclude,
            "max_files": max_files,
            "first_batch_size": batch_size,
        },
        "recipe_summary": {
            "tool": recipe["tool"],
            "language": recipe["language"],
            "summary": recipe["summary"],
            "risk_level": recipe["risk_level"],
        },
        "rollout_summary": {
            "batch_size": rollout["batch_size"],
            "branch_prefix": rollout["branch_prefix"],
            "pr_strategy": rollout["pr_strategy"],
            "planned_batches": max(1, (max_files + batch_size - 1) // batch_size),
        },
        "acceptance": campaign["acceptance"],
        "validation": campaign["validation"],
        "manual_review": campaign["manual_review"],
        "commands": commands,
        "warnings": warnings,
        "execution_policy": {
            "auto_apply": False,
            "auto_open_prs": False,
            "requires_human_review": True,
            "notes": [
                "All commands are advisory until explicitly approved.",
                "Do not run mutating codemod apply commands from this plan.",
            ],
        },
    }


def build_delegation_prompt(campaign: dict[str, Any], plan: dict[str, Any] | None = None) -> str:
    """Build a dispatch-ready prompt for campaign execution/review."""
    errors = validate_campaign(campaign)
    if errors:
        raise ValueError("invalid campaign: " + "; ".join(errors))
    plan = plan or build_dry_run_plan(campaign)
    meta = campaign["campaign"]

    lines = [
        "You are in the Orchestrator codemod/refactor-campaign lane.",
        "",
        f"Campaign: {meta['title']} ({meta['id']})",
        f"Goal: {meta['goal']}",
        "",
        campaign["delegate_prompt"].strip(),
        "",
        "Acceptance:",
        *[f"- {item}" for item in campaign["acceptance"]],
        "",
        "Validation:",
        *[f"- {item}" for item in campaign["validation"]],
        "",
        "Manual review required:",
        *[f"- {item}" for item in campaign["manual_review"]],
        "",
        "Dry-run policy:",
        "- Do NOT apply mutating codemod commands automatically.",
        "- Run only review_before_run commands after orchestrator approval.",
        "- Report changed paths and any non-mechanical follow-ups.",
        "",
        "Planned dry-run commands:",
    ]
    for cmd in plan["commands"]:
        if cmd.get("command"):
            lines.append(f"- [{cmd.get('purpose', 'command')}] `{cmd['command']}`")
    if plan.get("warnings"):
        lines.extend(["", "Plan warnings:"])
        lines.extend(f"- {warning}" for warning in plan["warnings"])
    return "\n".join(lines)


def _valid_campaign() -> dict[str, Any]:
    return json.loads(json.dumps(CAMPAIGN_SCHEMA_EXAMPLE))


def _selftest() -> None:
    prompt = build_authoring_prompt(
        goal="Rename LegacyHandler to ModernHandler across Python modules",
        repos=["owner/repo"],
        target="owner/repo#42",
        context="Preserve import paths unless the recipe requires otherwise.",
    )
    assert "refactor-campaign lane" in prompt and "ast-grep" in prompt, prompt
    assert '"delegate_prompt"' in prompt, prompt

    valid = _valid_campaign()
    assert validate_campaign(valid) == []
    plan = build_dry_run_plan(valid)
    assert plan["campaign_id"] == "rename-legacy-handler", plan
    assert plan["execution_policy"]["auto_apply"] is False, plan
    assert any(
        c["purpose"] == "scope_inventory" and "rg --files -g" in c["command"]
        for c in plan["commands"]
    ), plan
    recipe_cmds = [c for c in plan["commands"] if c.get("purpose") == "recipe_dry_run"]
    assert recipe_cmds and recipe_cmds[0]["review_before_run"] is True, plan
    assert recipe_cmds[0]["mutates_files"] is False, plan
    assert "ast-grep" in recipe_cmds[0]["command"], recipe_cmds[0]

    delegation = build_delegation_prompt(valid, plan)
    assert "Manual review required:" in delegation, delegation
    assert "Do NOT apply mutating" in delegation, delegation

    bad_slug = json.loads(json.dumps(valid))
    bad_slug["campaign"]["id"] = "Bad Slug"
    assert any("lowercase slug" in err for err in validate_campaign(bad_slug))

    bad_tool = json.loads(json.dumps(valid))
    bad_tool["recipe"]["tool"] = "sed"
    assert any("recipe.tool must be one of" in err for err in validate_campaign(bad_tool))

    bad_mechanism = json.loads(json.dumps(valid))
    bad_mechanism["recipe"].pop("match")
    bad_mechanism["recipe"].pop("rewrite")
    assert any("exactly one mechanism" in err for err in validate_campaign(bad_mechanism))

    dual_mechanism = json.loads(json.dumps(valid))
    dual_mechanism["recipe"]["rule_file"] = "rules/rename.yml"
    assert any("exactly one mechanism" in err for err in validate_campaign(dual_mechanism))

    comby = json.loads(json.dumps(valid))
    comby["recipe"] = {
        "tool": "comby",
        "language": "python",
        "summary": "Replace print with logging",
        "match": "print(:[x])",
        "rewrite": "logger.info(:[x])",
        "risk_level": "low",
    }
    assert validate_campaign(comby) == []
    comby_cmd = next(
        c for c in build_dry_run_plan(comby)["commands"] if c.get("purpose") == "recipe_dry_run"
    )
    assert "-diff" in comby_cmd["command"], comby_cmd

    custom_apply = json.loads(json.dumps(valid))
    custom_apply["recipe"] = {
        "tool": "custom",
        "language": "python",
        "summary": "Unsafe apply template",
        "command_template": "mytool --apply src/",
        "risk_level": "high",
    }
    assert validate_campaign(custom_apply) == []
    custom_plan = build_dry_run_plan(custom_apply)
    assert not any(
        c.get("purpose") == "recipe_dry_run" for c in custom_plan["commands"]
    ), custom_plan
    assert custom_plan["warnings"], custom_plan

    dry_custom = json.loads(json.dumps(valid))
    dry_custom["recipe"] = {
        "tool": "custom",
        "language": "python",
        "summary": "Safe preview template",
        "command_template": "mytool --dry-run src/",
        "risk_level": "low",
    }
    assert any(
        c.get("purpose") == "recipe_dry_run" for c in build_dry_run_plan(dry_custom)["commands"]
    )

    parsed = parse_campaign_json('```json\n{"campaign": {"id": "x"}}\n```')
    assert parsed == {"campaign": {"id": "x"}}, parsed
    print(
        "codemod_lane.py selftest: OK (authoring prompt, schema validation, dry-run plan, delegation prompt)"
    )


def _capability_heartbeat(event_type: str = "invocation") -> None:
    """Record that this capability ran, at its own code path.

    Infrastructure and lane capabilities are not always ROUTED to — they are entered directly — so
    each records use where it actually executes. Lazy import (capabilities imports feedback, and
    several of these are imported BY capabilities' dependencies), never raises (recording use must
    not be able to prevent the work), and inert outside an active tick via
    ORCH_CAPABILITY_HEARTBEATS. (2026-08-09)
    """
    try:
        import capabilities

        capabilities.production_heartbeat("codemod-campaign", event_type, ref="codemod_lane.main")
    except Exception:
        pass


def main(argv: Sequence[str]) -> int:
    _capability_heartbeat()
    parser = argparse.ArgumentParser(
        description="Build or validate an Orchestrator codemod campaign."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--goal", help="refactor/codemod goal to turn into an authoring prompt")
    group.add_argument("--goal-file", help="file containing the refactor/codemod goal")
    group.add_argument("--validate", help="campaign JSON file to validate")
    group.add_argument("--plan", help="campaign JSON file to turn into a dry-run plan")
    group.add_argument("--selftest", action="store_true", help="run offline selftests")
    parser.add_argument(
        "--repo", action="append", dest="repos", help="optional owner/repo context; repeatable"
    )
    parser.add_argument("--target", help="optional issue/PR target context")
    parser.add_argument(
        "--context", default="", help="inline context to include in the authoring prompt"
    )
    parser.add_argument(
        "--context-file", help="extra context file to include in the authoring prompt"
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    parser.add_argument(
        "--emit-delegation-prompt",
        action="store_true",
        help="with --plan, include a dispatch-ready delegation prompt",
    )
    args = parser.parse_args(list(argv))

    if args.selftest:
        _selftest()
        return 0

    if args.validate:
        try:
            campaign = parse_campaign_json(Path(args.validate).read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"Could not read or parse campaign JSON: {exc}", file=sys.stderr)
            return 2
        errors = validate_campaign(campaign)
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
            campaign = parse_campaign_json(Path(args.plan).read_text(encoding="utf-8"))
            plan = build_dry_run_plan(campaign)
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if args.json:
            payload: dict[str, Any] = {"plan": plan}
            if args.emit_delegation_prompt:
                payload["delegation_prompt"] = build_delegation_prompt(campaign, plan)
            print(json.dumps(payload, indent=2))
        else:
            print(json.dumps(plan, indent=2))
            if args.emit_delegation_prompt:
                print("\n--- delegation prompt ---\n")
                print(build_delegation_prompt(campaign, plan))
        return 0

    goal = args.goal or Path(args.goal_file).read_text(encoding="utf-8")
    prompt = build_authoring_prompt(
        goal=goal,
        repos=args.repos,
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
