#!/usr/bin/env python3
"""Build and validate Orchestrator cross-repo coordinated-change plans.

The cross_repo lane is the first increment for coordinated source+consumer
changes. It produces strict planning artifacts and dispatch prompts, but it does
not create branches, labels, issues, PRs, or merge barriers automatically.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Sequence

VALID_COMPATIBILITIES = {"backward-compatible", "breaking", "unknown"}
VALID_RISK_LEVELS = {"low", "medium", "high"}
VALID_ROLLOUT_STRATEGIES = {"source_first", "consumers_first", "draft_all_then_barrier", "manual"}
VALID_PR_MODES = {"draft", "ready"}

COORDINATION_SCHEMA_EXAMPLE = {
    "coordination": {
        "id": "workflows-context-api",
        "title": "Migrate Workflows Context API",
        "goal": "Add auth fields to the Workflows Context contract and update consumers",
        "source_repo": "stranske/Workflows",
        "constraints": ["Keep a compatibility window unless the plan explicitly marks the change breaking"],
        "definition_of_done": ["Source contract is implemented", "Every listed consumer validates against it"],
    },
    "contract_change": {
        "summary": "Introduce auth field on the Context object",
        "changed_interfaces": ["Workflows.Context.auth"],
        "source_files": ["src/workflows/context.py"],
        "compatibility": "backward-compatible",
        "migration_notes": ["Consumers should read auth through context.auth"],
    },
    "consumers": [
        {
            "repo": "stranske/Inv-Man-Intake",
            "reason": "Uses Workflows Context during intake authorization",
            "sync_manifest_refs": ["sync-manifest.json#workflows"],
            "required_changes": ["Update auth helper to read context.auth"],
            "validation": ["pytest tests/test_auth.py"],
            "risk_level": "medium",
        }
    ],
    "rollout": {
        "strategy": "source_first",
        "branch_prefix": "cross-repo/workflows-context-api",
        "pr_mode": "draft",
        "merge_order": ["stranske/Workflows", "stranske/Inv-Man-Intake"],
        "barrier_checks": ["Source PR merged or released", "Consumer tests pass against the source contract"],
        "rollback_plan": ["Revert source contract PR", "Revert or close consumer PRs"],
    },
    "prompts": {
        "source_prompt": "Implement the Context auth field in Workflows and keep compatibility.",
        "consumer_prompt_template": "Update {repo} to consume the Workflows Context auth field.",
        "review_prompt": "Review {repo} against the cross-repo contract and validation plan.",
    },
}


def _read_optional(path: str | Path | None) -> str:
    if not path:
        return ""
    return Path(path).read_text(encoding="utf-8")


def build_authoring_prompt(
    *,
    goal: str,
    source_repo: str | None = None,
    consumers: Sequence[str] | None = None,
    target: str | None = None,
    context: str = "",
    context_file: str | Path | None = None,
) -> str:
    """Return the prompt for an offloaded coordination-authoring pass."""
    if not goal.strip():
        raise ValueError("goal must be non-empty")

    file_context = _read_optional(context_file)
    context_parts = [part.strip() for part in (context, file_context) if part and part.strip()]
    context_block = "\n\nAdditional context:\n" + "\n\n".join(context_parts) if context_parts else ""
    source_block = f"\nSource repository: {source_repo}" if source_repo else ""
    consumers_block = "\nConsumer repositories: " + ", ".join(consumers) if consumers else ""
    target_block = f"\nTarget: {target}" if target else ""
    schema = json.dumps(COORDINATION_SCHEMA_EXAMPLE, indent=2)

    return f"""You are in the Orchestrator cross-repo coordination lane.

Goal:
{goal.strip()}{source_block}{consumers_block}{target_block}{context_block}

Author a conservative coordinated source+consumer change plan. This lane is for
contract changes such as Workflows changes that must land with consumer updates.
Use sync-manifest or dependency-graph context when it is available.

Return exactly one JSON object and no prose. The object must match this shape:

{schema}

Rules:
- coordination.id must be a stable lowercase slug.
- coordination.source_repo must be the contract source repository in owner/repo form.
- consumers must list every affected consumer repo exactly once.
- contract_change.compatibility must be one of: backward-compatible, breaking, unknown.
- rollout.strategy must be one of: source_first, consumers_first, draft_all_then_barrier, manual.
- rollout.merge_order must include the source repo and every consumer repo exactly once.
- rollout.pr_mode must be draft or ready; prefer draft for coordinated changes.
- prompts.consumer_prompt_template must contain {{repo}}.
- prompts.review_prompt should be usable for each consumer and may contain {{repo}}.
- Do not include mutating shell commands. This first increment only plans and validates.
"""


def _is_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _looks_like_slug(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]*", value))


def _looks_like_repo(value: str) -> bool:
    return bool(re.fullmatch(r"[^/\s]+/[^/\s]+", value))


def _repo_slug(repo: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", repo.lower()).strip("-")


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


def validate_coordination(plan: dict[str, Any]) -> list[str]:
    """Validate an agent-produced cross-repo coordination plan."""
    errors: list[str] = []
    if not isinstance(plan, dict):
        return ["coordination plan must be a JSON object"]

    required_top = ("coordination", "contract_change", "consumers", "rollout", "prompts")
    errors.extend(_validate_required_mapping(plan, "plan", required_top))
    if errors:
        return errors

    meta = plan["coordination"]
    errors.extend(_validate_required_mapping(
        meta,
        "coordination",
        ("id", "title", "goal", "source_repo", "constraints", "definition_of_done"),
    ))
    source_repo = None
    if isinstance(meta, dict):
        coord_id = meta.get("id")
        if not _is_nonempty_string(coord_id):
            errors.append("coordination.id must be a non-empty string")
        elif not _looks_like_slug(coord_id.strip()):
            errors.append("coordination.id must be a lowercase slug")
        for key in ("title", "goal", "source_repo"):
            if not _is_nonempty_string(meta.get(key)):
                errors.append(f"coordination.{key} must be a non-empty string")
        source_repo = meta.get("source_repo") if _is_nonempty_string(meta.get("source_repo")) else None
        if source_repo and not _looks_like_repo(source_repo):
            errors.append("coordination.source_repo must look like owner/repo")
        errors.extend(_validate_string_list(meta.get("constraints"), "coordination.constraints", nonempty=False))
        errors.extend(_validate_string_list(meta.get("definition_of_done"), "coordination.definition_of_done", nonempty=True))

    contract = plan["contract_change"]
    errors.extend(_validate_required_mapping(
        contract,
        "contract_change",
        ("summary", "changed_interfaces", "source_files", "compatibility", "migration_notes"),
    ))
    if isinstance(contract, dict):
        if not _is_nonempty_string(contract.get("summary")):
            errors.append("contract_change.summary must be a non-empty string")
        errors.extend(_validate_string_list(contract.get("changed_interfaces"), "contract_change.changed_interfaces", nonempty=True))
        errors.extend(_validate_string_list(contract.get("source_files"), "contract_change.source_files", nonempty=True))
        if contract.get("compatibility") not in VALID_COMPATIBILITIES:
            errors.append(f"contract_change.compatibility must be one of {sorted(VALID_COMPATIBILITIES)}")
        errors.extend(_validate_string_list(contract.get("migration_notes"), "contract_change.migration_notes", nonempty=True))

    consumers = plan["consumers"]
    consumer_repos: list[str] = []
    if not isinstance(consumers, list):
        errors.append("consumers must be a list")
        consumers = []
    elif not consumers:
        errors.append("consumers must be a non-empty list")
    for idx, consumer in enumerate(consumers):
        path = f"consumers[{idx}]"
        errors.extend(_validate_required_mapping(
            consumer,
            path,
            ("repo", "reason", "sync_manifest_refs", "required_changes", "validation", "risk_level"),
        ))
        if not isinstance(consumer, dict):
            continue
        repo = consumer.get("repo")
        if not _is_nonempty_string(repo):
            errors.append(f"{path}.repo must be a non-empty string")
        elif not _looks_like_repo(repo):
            errors.append(f"{path}.repo must look like owner/repo")
        else:
            consumer_repos.append(repo)
        if not _is_nonempty_string(consumer.get("reason")):
            errors.append(f"{path}.reason must be a non-empty string")
        errors.extend(_validate_string_list(consumer.get("sync_manifest_refs"), f"{path}.sync_manifest_refs", nonempty=False))
        errors.extend(_validate_string_list(consumer.get("required_changes"), f"{path}.required_changes", nonempty=True))
        errors.extend(_validate_string_list(consumer.get("validation"), f"{path}.validation", nonempty=True))
        if consumer.get("risk_level") not in VALID_RISK_LEVELS:
            errors.append(f"{path}.risk_level must be one of {sorted(VALID_RISK_LEVELS)}")

    if len(consumer_repos) != len(set(consumer_repos)):
        errors.append("consumers.repo values must be unique")
    if source_repo and source_repo in consumer_repos:
        errors.append("source_repo must not also be listed as a consumer")

    rollout = plan["rollout"]
    errors.extend(_validate_required_mapping(
        rollout,
        "rollout",
        ("strategy", "branch_prefix", "pr_mode", "merge_order", "barrier_checks", "rollback_plan"),
    ))
    if isinstance(rollout, dict):
        if rollout.get("strategy") not in VALID_ROLLOUT_STRATEGIES:
            errors.append(f"rollout.strategy must be one of {sorted(VALID_ROLLOUT_STRATEGIES)}")
        if not _is_nonempty_string(rollout.get("branch_prefix")):
            errors.append("rollout.branch_prefix must be a non-empty string")
        if rollout.get("pr_mode") not in VALID_PR_MODES:
            errors.append(f"rollout.pr_mode must be one of {sorted(VALID_PR_MODES)}")
        errors.extend(_validate_string_list(rollout.get("merge_order"), "rollout.merge_order", nonempty=True))
        errors.extend(_validate_string_list(rollout.get("barrier_checks"), "rollout.barrier_checks", nonempty=True))
        errors.extend(_validate_string_list(rollout.get("rollback_plan"), "rollout.rollback_plan", nonempty=True))
        if isinstance(rollout.get("merge_order"), list) and source_repo:
            merge_order = [item.strip() for item in rollout["merge_order"] if isinstance(item, str)]
            expected = {source_repo, *consumer_repos}
            actual = set(merge_order)
            if actual != expected or len(merge_order) != len(actual):
                errors.append("rollout.merge_order must include source_repo and every consumer repo exactly once")

    prompts = plan["prompts"]
    errors.extend(_validate_required_mapping(
        prompts,
        "prompts",
        ("source_prompt", "consumer_prompt_template", "review_prompt"),
    ))
    if isinstance(prompts, dict):
        if not _is_nonempty_string(prompts.get("source_prompt")):
            errors.append("prompts.source_prompt must be a non-empty string")
        template = prompts.get("consumer_prompt_template")
        if not _is_nonempty_string(template):
            errors.append("prompts.consumer_prompt_template must be a non-empty string")
        elif "{repo}" not in template:
            errors.append("prompts.consumer_prompt_template must contain '{repo}'")
        if not _is_nonempty_string(prompts.get("review_prompt")):
            errors.append("prompts.review_prompt must be a non-empty string")
    return errors


def parse_coordination_json(content: str) -> dict[str, Any]:
    """Parse coordination JSON, accepting one optional Markdown code fence."""
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
        raise ValueError("coordination JSON must decode to an object")
    return parsed


def build_source_dispatch_prompt(plan: dict[str, Any]) -> str:
    meta = plan["coordination"]
    contract = plan["contract_change"]
    prompts = plan["prompts"]
    return "\n".join([
        "You are in the Orchestrator cross-repo lane (source role).",
        "",
        f"Campaign: {meta['title']} ({meta['id']})",
        f"Source repo: {meta['source_repo']}",
        f"Goal: {meta['goal']}",
        "",
        prompts["source_prompt"].strip(),
        "",
        "Contract change:",
        f"- Summary: {contract['summary']}",
        f"- Changed interfaces: {', '.join(contract['changed_interfaces'])}",
        f"- Source files: {', '.join(contract['source_files'])}",
        f"- Compatibility: {contract['compatibility']}",
        "",
        "Migration notes:",
        *[f"- {note}" for note in contract["migration_notes"]],
        "",
        "Definition of done:",
        *[f"- {item}" for item in meta["definition_of_done"]],
    ])


def build_consumer_dispatch_prompt(plan: dict[str, Any], consumer: dict[str, Any]) -> str:
    meta = plan["coordination"]
    contract = plan["contract_change"]
    template = plan["prompts"]["consumer_prompt_template"].format(repo=consumer["repo"])
    return "\n".join([
        "You are in the Orchestrator cross-repo lane (consumer role).",
        "",
        f"Campaign: {meta['title']} ({meta['id']})",
        f"Consumer repo: {consumer['repo']}",
        f"Goal: {meta['goal']}",
        "",
        template.strip(),
        "",
        "Required changes:",
        *[f"- {item}" for item in consumer["required_changes"]],
        "",
        "Contract context:",
        f"- Summary: {contract['summary']}",
        f"- Compatibility: {contract['compatibility']}",
        "",
        "Validation:",
        *[f"- {item}" for item in consumer["validation"]],
    ])


def build_review_prompt(plan: dict[str, Any], consumer: dict[str, Any]) -> str:
    meta = plan["coordination"]
    review_template = plan["prompts"]["review_prompt"].format(repo=consumer["repo"])
    return "\n".join([
        "You are in the Orchestrator cross-repo lane (review role).",
        "",
        f"Campaign: {meta['title']} ({meta['id']})",
        f"Consumer repo: {consumer['repo']}",
        "",
        review_template.strip(),
        "",
        "Validation to check:",
        *[f"- {item}" for item in consumer["validation"]],
    ])


def _dependencies_for(strategy: str, item_id: str, consumer_ids: list[str]) -> list[str]:
    if strategy == "source_first" and item_id.startswith("consumer:"):
        return ["source"]
    if strategy == "consumers_first" and item_id == "source":
        return consumer_ids
    return []


def validate_deterministic_barriers(
    step_ids: Sequence[str], barriers: Sequence[dict[str, Any]]
) -> list[str]:
    """Validate the typed before/after barrier subset used by compiled rails."""
    errors: list[str] = []
    known = set(step_ids)
    seen: set[str] = set()
    for index, barrier in enumerate(barriers):
        path = f"barriers[{index}]"
        if not isinstance(barrier, dict):
            errors.append(f"{path} must be an object")
            continue
        barrier_id = str(barrier.get("id") or "").strip()
        after = str(barrier.get("after") or "").strip()
        before = str(barrier.get("before") or "").strip()
        condition_id = str(barrier.get("condition_id") or "").strip()
        if not barrier_id:
            errors.append(f"{path}.id is required")
        elif barrier_id in seen:
            errors.append("duplicate barrier ID")
        seen.add(barrier_id)
        if after not in known:
            errors.append(f"{path}.after references unknown step {after!r}")
        if before not in known:
            errors.append(f"{path}.before references unknown step {before!r}")
        if after and before and after == before:
            errors.append(f"{path} cannot order a step against itself")
        if not condition_id:
            errors.append(f"{path}.condition_id is required")
        unknown = sorted(set(barrier) - {"id", "after", "before", "condition_id"})
        if unknown:
            errors.append(f"{path} has unsupported fields: {unknown}")
    return errors


def build_dry_run_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Produce a dry-run rollout plan without side effects."""
    errors = validate_coordination(plan)
    if errors:
        raise ValueError("invalid coordination: " + "; ".join(errors))

    meta = plan["coordination"]
    consumers = plan["consumers"]
    rollout = plan["rollout"]
    strategy = rollout["strategy"]
    consumer_ids = [f"consumer:{_repo_slug(consumer['repo'])}" for consumer in consumers]

    planned_work_items: list[dict[str, Any]] = [{
        "id": "source",
        "repo": meta["source_repo"],
        "role": "source",
        "lane": "opener",
        "task_type": "implement",
        "branch": f"{rollout['branch_prefix']}/source-{meta['id']}",
        "depends_on": _dependencies_for(strategy, "source", consumer_ids),
        "validation": ["Run source repo validation from the source task prompt"],
    }]
    for consumer, item_id in zip(consumers, consumer_ids):
        planned_work_items.append({
            "id": item_id,
            "repo": consumer["repo"],
            "role": "consumer",
            "lane": "opener",
            "task_type": "implement",
            "branch": f"{rollout['branch_prefix']}/{_repo_slug(consumer['repo'])}-{meta['id']}",
            "depends_on": _dependencies_for(strategy, item_id, consumer_ids),
            "validation": consumer["validation"],
            "risk_level": consumer["risk_level"],
        })

    dispatch_ready_prompts = [{
        "item_id": "source",
        "repo": meta["source_repo"],
        "lane": "opener",
        "task_type": "implement",
        "prompt": build_source_dispatch_prompt(plan),
    }]
    for consumer, item_id in zip(consumers, consumer_ids):
        dispatch_ready_prompts.append({
            "item_id": item_id,
            "repo": consumer["repo"],
            "lane": "opener",
            "task_type": "implement",
            "prompt": build_consumer_dispatch_prompt(plan, consumer),
        })
        dispatch_ready_prompts.append({
            "item_id": f"review:{_repo_slug(consumer['repo'])}",
            "repo": consumer["repo"],
            "lane": "opener",
            "task_type": "review",
            "prompt": build_review_prompt(plan, consumer),
        })

    return {
        "coordination_id": meta["id"],
        "title": meta["title"],
        "goal": meta["goal"],
        "source_repo": meta["source_repo"],
        "consumers": [consumer["repo"] for consumer in consumers],
        "planned_work_items": planned_work_items,
        "barrier": {
            "strategy": strategy,
            "merge_order": rollout["merge_order"],
            "barrier_checks": rollout["barrier_checks"],
            "rollback_plan": rollout["rollback_plan"],
            "requires_manual_release_decision": True,
        },
        "dispatch_ready_prompts": dispatch_ready_prompts,
        "execution_policy": {
            "auto_create_issues": False,
            "auto_create_branches": False,
            "auto_create_prs": False,
            "auto_merge": False,
            "requires_barrier_review": True,
            "notes": [
                "Dry-run only: no GitHub branches, labels, issues, PRs, or merges are created automatically.",
                "Use dispatch_ready_prompts only after reviewing dependencies and barrier checks.",
            ],
        },
    }


def _valid_plan() -> dict[str, Any]:
    return json.loads(json.dumps(COORDINATION_SCHEMA_EXAMPLE))


def _selftest() -> None:
    prompt = build_authoring_prompt(
        goal="Change Context API in Workflows",
        source_repo="stranske/Workflows",
        consumers=["stranske/Inv-Man-Intake"],
        target="stranske/Workflows#123",
        context="Keep changes backward compatible if possible.",
    )
    assert "cross-repo coordination lane" in prompt, prompt
    assert '"contract_change"' in prompt and "stranske/Inv-Man-Intake" in prompt, prompt

    valid = _valid_plan()
    assert validate_coordination(valid) == []
    dry_run = build_dry_run_plan(valid)
    assert dry_run["coordination_id"] == "workflows-context-api", dry_run
    assert dry_run["execution_policy"]["auto_create_prs"] is False, dry_run
    assert dry_run["execution_policy"]["requires_barrier_review"] is True, dry_run
    assert len(dry_run["planned_work_items"]) == 2, dry_run["planned_work_items"]
    assert dry_run["planned_work_items"][1]["depends_on"] == ["source"], dry_run["planned_work_items"]
    assert len(dry_run["dispatch_ready_prompts"]) == 3, dry_run["dispatch_ready_prompts"]

    bad_slug = json.loads(json.dumps(valid))
    bad_slug["coordination"]["id"] = "Bad Slug"
    assert any("lowercase slug" in err for err in validate_coordination(bad_slug))

    bad_compat = json.loads(json.dumps(valid))
    bad_compat["contract_change"]["compatibility"] = "maybe"
    assert any("contract_change.compatibility" in err for err in validate_coordination(bad_compat))

    duplicate = json.loads(json.dumps(valid))
    duplicate["consumers"].append(json.loads(json.dumps(valid["consumers"][0])))
    assert any("unique" in err for err in validate_coordination(duplicate))

    bad_order = json.loads(json.dumps(valid))
    bad_order["rollout"]["merge_order"] = ["stranske/Workflows"]
    assert any("merge_order" in err for err in validate_coordination(bad_order))

    bad_template = json.loads(json.dumps(valid))
    bad_template["prompts"]["consumer_prompt_template"] = "Update the consumer"
    assert any("consumer_prompt_template must contain" in err for err in validate_coordination(bad_template))

    parsed = parse_coordination_json("```json\n{\"coordination\": {\"id\": \"x\"}}\n```")
    assert parsed == {"coordination": {"id": "x"}}, parsed
    print("cross_repo_lane.py selftest: OK (authoring prompt, schema validation, dry-run plan, dispatch prompts)")


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
        capabilities.production_heartbeat("cross-repo-coordination", event_type, ref="cross_repo_lane.main")
    except Exception:
        pass


def main(argv: Sequence[str]) -> int:
    _capability_heartbeat()
    parser = argparse.ArgumentParser(description="Build or validate an Orchestrator cross-repo coordination plan.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--goal", help="coordination goal to turn into an authoring prompt")
    group.add_argument("--goal-file", help="file containing the coordination goal")
    group.add_argument("--validate", help="coordination JSON file to validate")
    group.add_argument("--plan", help="coordination JSON file to turn into a dry-run plan")
    group.add_argument("--selftest", action="store_true", help="run offline selftests")
    parser.add_argument("--source-repo", help="optional contract source repo context")
    parser.add_argument("--consumer", action="append", dest="consumers", help="optional consumer repo; repeatable")
    parser.add_argument("--target", help="optional issue/PR target context")
    parser.add_argument("--context", default="", help="inline context to include in the authoring prompt")
    parser.add_argument("--context-file", help="extra context file to include in the authoring prompt")
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    parser.add_argument("--emit-dispatch-prompts", action="store_true",
                        help="with --plan, include dispatch-ready prompts")
    args = parser.parse_args(list(argv))

    if args.selftest:
        _selftest()
        return 0

    if args.validate:
        try:
            plan = parse_coordination_json(Path(args.validate).read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"Could not read or parse coordination JSON: {exc}", file=sys.stderr)
            return 2
        errors = validate_coordination(plan)
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
            plan = parse_coordination_json(Path(args.plan).read_text(encoding="utf-8"))
            dry_run = build_dry_run_plan(plan)
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if args.json:
            payload: dict[str, Any] = {"plan": dry_run}
            if not args.emit_dispatch_prompts:
                payload["plan"] = {key: value for key, value in dry_run.items() if key != "dispatch_ready_prompts"}
            else:
                payload["dispatch_ready_prompts"] = dry_run["dispatch_ready_prompts"]
            print(json.dumps(payload, indent=2))
        else:
            if args.emit_dispatch_prompts:
                print(json.dumps(dry_run, indent=2))
            else:
                print(json.dumps({key: value for key, value in dry_run.items() if key != "dispatch_ready_prompts"}, indent=2))
        return 0

    goal = args.goal or Path(args.goal_file).read_text(encoding="utf-8")
    prompt = build_authoring_prompt(
        goal=goal,
        source_repo=args.source_repo,
        consumers=args.consumers,
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

