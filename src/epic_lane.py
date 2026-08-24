#!/usr/bin/env python3
"""Build and validate Orchestrator epic decomposition plans.

The epic lane is the first increment for taking a large or vague goal and turning
it into a structured subtask plan that can later be dispatched, monitored, and
re-decomposed when a slice stalls.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

VALID_LANES = {"opener", "closer", "local"}
VALID_TASK_TYPES = {"mechanical", "implement", "testgen", "polish", "review", "epic"}


PLAN_SCHEMA_EXAMPLE = {
    "epic": {
        "title": "Concise epic title",
        "goal": "The large or vague goal being decomposed",
        "repo": "optional owner/repo or null",
        "constraints": ["Constraints the plan must preserve"],
        "definition_of_done": ["Epic-level done condition"],
    },
    "subtasks": [
        {
            "id": "E1",
            "title": "Subtask title",
            "lane": "opener|closer|local",
            "task_type": "implement|testgen|mechanical|polish|review|epic",
            "scope": "Bounded scope for this subtask",
            "files": ["expected/path.py"],
            "acceptance": ["What must be true for this subtask to count"],
            "validation": ["Exact tests, commands, or checks to run"],
            "dependencies": [],
            "dispatch_prompt": "Prompt to hand to the delegated agent",
            "redecompose_if": ["Concrete stall/failure trigger for splitting this subtask"],
        }
    ],
    "integration": {
        "order": ["E1"],
        "risks": ["Integration risk and mitigation"],
        "final_verification": ["Epic-level verification command or inspection"],
    },
    "re_decomposition_triggers": ["Top-level condition that should invalidate or split the plan"],
}


def _read_optional(path: str | Path | None) -> str:
    if not path:
        return ""
    return Path(path).read_text(encoding="utf-8")


def build_planner_prompt(
    *,
    goal: str,
    repo: str | None = None,
    target: str | None = None,
    context: str = "",
    context_file: str | Path | None = None,
    subtask_count: int | None = None,
) -> str:
    """Return the prompt for an offloaded planner pass."""
    if not goal.strip():
        raise ValueError("goal must be non-empty")
    if subtask_count is not None and subtask_count < 1:
        raise ValueError("subtask_count must be >= 1")

    file_context = _read_optional(context_file)
    context_parts = [part.strip() for part in (context, file_context) if part and part.strip()]
    context_block = (
        "\n\nAdditional context:\n" + "\n\n".join(context_parts) if context_parts else ""
    )
    repo_block = f"\nRepository: {repo}" if repo else ""
    target_block = f"\nTarget: {target}" if target else ""
    count_block = f"\nTarget subtask count: {subtask_count}" if subtask_count else ""
    schema = json.dumps(PLAN_SCHEMA_EXAMPLE, indent=2)

    return f"""You are in the Orchestrator epic decomposition lane.

Goal:
{goal.strip()}{repo_block}{target_block}{count_block}{context_block}

Use a Plan-and-Solve planner pass to identify the major slices, then add ADaPT-style
re-decomposition triggers for subtasks that are likely to stall, fail CI, or expose
new ambiguity. Prefer structured handoffs over implementation detail.

Return exactly one JSON object and no prose. The object must match this shape:

{schema}

Rules:
- Subtask IDs must be stable strings such as E1, E2, E3.
- Every subtask must be dispatchable by itself and have concrete acceptance and validation lists.
- Use lane "local" for work the orchestrator should keep, such as final synthesis, plan review, or manual integration.
- Use task_type "epic" only for a subtask that must itself be recursively decomposed.
- integration.order must contain every subtask ID exactly once in the intended execution/integration order.
- dependencies may only reference existing subtask IDs.
- redecompose_if and re_decomposition_triggers must name observable conditions, not vague concerns.
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


def validate_plan(plan: dict[str, Any]) -> list[str]:
    """Validate an agent-produced epic plan.

    Returns a list of errors. An empty list means the plan is valid enough for
    orchestrator review and possible dispatch.
    """
    errors: list[str] = []
    if not isinstance(plan, dict):
        return ["plan must be a JSON object"]

    required_top = ("epic", "subtasks", "integration", "re_decomposition_triggers")
    errors.extend(_validate_required_mapping(plan, "plan", required_top))
    if errors:
        return errors

    epic = plan["epic"]
    errors.extend(
        _validate_required_mapping(
            epic, "epic", ("title", "goal", "repo", "constraints", "definition_of_done")
        )
    )
    if isinstance(epic, dict):
        if not _is_nonempty_string(epic.get("title")):
            errors.append("epic.title must be a non-empty string")
        if not _is_nonempty_string(epic.get("goal")):
            errors.append("epic.goal must be a non-empty string")
        if epic.get("repo") is not None and not isinstance(epic.get("repo"), str):
            errors.append("epic.repo must be a string or null")
        errors.extend(
            _validate_string_list(epic.get("constraints"), "epic.constraints", nonempty=False)
        )
        errors.extend(
            _validate_string_list(
                epic.get("definition_of_done"), "epic.definition_of_done", nonempty=True
            )
        )

    subtasks = plan["subtasks"]
    if not isinstance(subtasks, list):
        errors.append("subtasks must be a list")
        subtasks = []
    elif not subtasks:
        errors.append("subtasks must be a non-empty list")

    ids: list[str] = []
    seen: set[str] = set()
    required_task = (
        "id",
        "title",
        "lane",
        "task_type",
        "scope",
        "files",
        "acceptance",
        "validation",
        "dependencies",
        "dispatch_prompt",
        "redecompose_if",
    )
    for idx, task in enumerate(subtasks):
        path = f"subtasks[{idx}]"
        task_required_errors = _validate_required_mapping(task, path, required_task)
        errors.extend(task_required_errors)
        if not isinstance(task, dict) or task_required_errors:
            continue

        task_id = task.get("id")
        if not _is_nonempty_string(task_id):
            errors.append(f"{path}.id must be a non-empty string")
        else:
            normalized_id = task_id.strip()
            if normalized_id in seen:
                errors.append(f"{path}.id duplicates {normalized_id!r}")
            seen.add(normalized_id)
            ids.append(normalized_id)

        if not _is_nonempty_string(task.get("title")):
            errors.append(f"{path}.title must be a non-empty string")
        if task.get("lane") not in VALID_LANES:
            errors.append(f"{path}.lane must be one of {sorted(VALID_LANES)}")
        if task.get("task_type") not in VALID_TASK_TYPES:
            errors.append(f"{path}.task_type must be one of {sorted(VALID_TASK_TYPES)}")
        if not _is_nonempty_string(task.get("scope")):
            errors.append(f"{path}.scope must be a non-empty string")
        errors.extend(_validate_string_list(task.get("files"), f"{path}.files", nonempty=False))
        errors.extend(
            _validate_string_list(task.get("acceptance"), f"{path}.acceptance", nonempty=True)
        )
        errors.extend(
            _validate_string_list(task.get("validation"), f"{path}.validation", nonempty=True)
        )
        errors.extend(
            _validate_string_list(task.get("dependencies"), f"{path}.dependencies", nonempty=False)
        )
        if not _is_nonempty_string(task.get("dispatch_prompt")):
            errors.append(f"{path}.dispatch_prompt must be a non-empty string")
        errors.extend(
            _validate_string_list(
                task.get("redecompose_if"), f"{path}.redecompose_if", nonempty=True
            )
        )

    known_ids = set(ids)
    for idx, task in enumerate(subtasks):
        if not isinstance(task, dict) or not isinstance(task.get("dependencies"), list):
            continue
        for dep_idx, dep in enumerate(task["dependencies"]):
            if isinstance(dep, str) and dep.strip() not in known_ids:
                errors.append(
                    f"subtasks[{idx}].dependencies[{dep_idx}] references unknown subtask ID {dep!r}"
                )

    integration = plan["integration"]
    errors.extend(
        _validate_required_mapping(
            integration, "integration", ("order", "risks", "final_verification")
        )
    )
    if isinstance(integration, dict):
        order = integration.get("order")
        errors.extend(_validate_string_list(order, "integration.order", nonempty=True))
        if isinstance(order, list) and all(isinstance(item, str) for item in order):
            order_ids = [item.strip() for item in order]
            for order_idx, item in enumerate(order_ids):
                if item not in known_ids:
                    errors.append(
                        f"integration.order[{order_idx}] references unknown subtask ID {item!r}"
                    )
            if len(order_ids) != len(set(order_ids)):
                errors.append("integration.order must not contain duplicate subtask IDs")
            if known_ids and set(order_ids) != known_ids:
                missing = sorted(known_ids - set(order_ids))
                extra = sorted(set(order_ids) - known_ids)
                if missing:
                    errors.append(f"integration.order is missing subtask IDs: {missing}")
                if extra:
                    errors.append(f"integration.order contains unknown subtask IDs: {extra}")
        errors.extend(
            _validate_string_list(integration.get("risks"), "integration.risks", nonempty=False)
        )
        errors.extend(
            _validate_string_list(
                integration.get("final_verification"),
                "integration.final_verification",
                nonempty=True,
            )
        )

    errors.extend(
        _validate_string_list(
            plan.get("re_decomposition_triggers"), "re_decomposition_triggers", nonempty=True
        )
    )
    return errors


def validate_dependency_dag(nodes: Sequence[dict[str, Any]]) -> list[str]:
    """Validate the deterministic ``id``/``depends_on`` subset shared by rails.

    Epic plans retain their richer authoring contract.  The capability compiler
    deliberately reuses only this small graph rail, so generated prose can never
    become an executable dependency or command.
    """
    errors: list[str] = []
    ids = [str(node.get("id") or "").strip() for node in nodes if isinstance(node, dict)]
    if len(ids) != len(nodes) or any(not item for item in ids):
        errors.append("DAG nodes require non-empty IDs")
        return errors
    if len(ids) != len(set(ids)):
        errors.append("duplicate DAG node ID")
        return errors
    known = set(ids)
    dependencies: dict[str, tuple[str, ...]] = {}
    for node in nodes:
        node_id = str(node["id"]).strip()
        raw = node.get("depends_on")
        if not isinstance(raw, list):
            errors.append(f"{node_id}.depends_on must be a list")
            continue
        deps = tuple(str(item).strip() for item in raw)
        if any(not item for item in deps):
            errors.append(f"{node_id}.depends_on contains an empty ID")
        if len(deps) != len(set(deps)):
            errors.append(f"{node_id}.depends_on contains duplicates")
        for dep in deps:
            if dep not in known:
                errors.append(f"{node_id}.depends_on references unknown node {dep!r}")
        dependencies[node_id] = deps
    if errors:
        return errors

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> bool:
        if node_id in visiting:
            return False
        if node_id in visited:
            return True
        visiting.add(node_id)
        for dependency in dependencies[node_id]:
            if not visit(dependency):
                return False
        visiting.remove(node_id)
        visited.add(node_id)
        return True

    if any(not visit(node_id) for node_id in ids):
        errors.append("cycle detected")
    return errors


def dependency_order(nodes: Sequence[dict[str, Any]]) -> list[str]:
    """Return one stable topological order or reject an invalid dependency DAG."""
    errors = validate_dependency_dag(nodes)
    if errors:
        raise ValueError("; ".join(errors))
    by_id = {str(node["id"]).strip(): node for node in nodes}
    emitted: set[str] = set()
    order: list[str] = []
    while len(order) < len(by_id):
        ready = sorted(
            node_id
            for node_id, node in by_id.items()
            if node_id not in emitted
            and set(str(dep).strip() for dep in node.get("depends_on") or []) <= emitted
        )
        if not ready:  # Defensive: validate_dependency_dag already detects this.
            raise ValueError("cycle detected")
        order.extend(ready)
        emitted.update(ready)
    return order


def parse_plan_json(content: str) -> dict[str, Any]:
    """Parse a plan JSON object, accepting a single optional Markdown code fence."""
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
        raise ValueError("plan JSON must decode to an object")
    return parsed


def build_dispatch_prompts(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Return dispatch-ready prompt records for validated subtasks."""
    errors = validate_plan(plan)
    if errors:
        raise ValueError("invalid plan: " + "; ".join(errors))

    records: list[dict[str, Any]] = []
    epic = plan["epic"]
    for task in plan["subtasks"]:
        prompt = "\n".join(
            [
                f"Epic: {epic['title']}",
                f"Subtask {task['id']}: {task['title']}",
                "",
                task["dispatch_prompt"].strip(),
                "",
                "Acceptance:",
                *[f"- {item}" for item in task["acceptance"]],
                "",
                "Validation:",
                *[f"- {item}" for item in task["validation"]],
                "",
                "Re-decompose this subtask if:",
                *[f"- {item}" for item in task["redecompose_if"]],
            ]
        )
        records.append(
            {
                "id": task["id"],
                "lane": task["lane"],
                "task_type": task["task_type"],
                "dependencies": task["dependencies"],
                "prompt": prompt,
            }
        )
    return records


def _valid_plan() -> dict[str, Any]:
    return {
        "epic": {
            "title": "Add course analytics",
            "goal": "Give instructors useful learner-progress analytics",
            "repo": "stranske/learning-management-system",
            "constraints": ["Keep existing learner flows working"],
            "definition_of_done": ["Instructor can inspect course progress", "Focused tests pass"],
        },
        "subtasks": [
            {
                "id": "E1",
                "title": "Data model and queries",
                "lane": "opener",
                "task_type": "implement",
                "scope": "Add analytics aggregation queries",
                "files": ["app/models.py", "tests/test_analytics.py"],
                "acceptance": ["Queries return progress counts for a seeded course"],
                "validation": ["pytest tests/test_analytics.py"],
                "dependencies": [],
                "dispatch_prompt": "Implement the analytics data model and query layer.",
                "redecompose_if": [
                    "The existing schema cannot represent progress without a migration"
                ],
            },
            {
                "id": "E2",
                "title": "Instructor UI",
                "lane": "opener",
                "task_type": "implement",
                "scope": "Render analytics in instructor course view",
                "files": ["app/templates/course.html", "tests/test_course_view.py"],
                "acceptance": ["Instructor view shows the analytics summary"],
                "validation": ["pytest tests/test_course_view.py"],
                "dependencies": ["E1"],
                "dispatch_prompt": "Build the instructor-facing analytics UI.",
                "redecompose_if": ["The UI requires a broader navigation redesign"],
            },
            {
                "id": "E3",
                "title": "Final synthesis",
                "lane": "local",
                "task_type": "review",
                "scope": "Integrate and verify the epic",
                "files": [],
                "acceptance": ["All prior subtasks are integrated"],
                "validation": ["pytest"],
                "dependencies": ["E1", "E2"],
                "dispatch_prompt": "Review the combined analytics epic against definition of done.",
                "redecompose_if": ["Integration reveals a new cross-cutting migration requirement"],
            },
        ],
        "integration": {
            "order": ["E1", "E2", "E3"],
            "risks": ["UI may depend on aggregation semantics"],
            "final_verification": [
                "pytest",
                "Run frontend verifier against instructor course page if served locally",
            ],
        },
        "re_decomposition_triggers": ["A subtask fails validation twice for different root causes"],
    }


def _selftest() -> None:
    prompt = build_planner_prompt(
        goal="Add instructor analytics to the LMS",
        repo="stranske/learning-management-system",
        target="stranske/learning-management-system#999",
        context="Preserve existing course authoring flows.",
        subtask_count=3,
    )
    assert "Plan-and-Solve" in prompt and "ADaPT-style" in prompt, prompt
    assert "Target subtask count: 3" in prompt, prompt
    assert '"epic"' in prompt and '"re_decomposition_triggers"' in prompt, prompt

    valid = _valid_plan()
    assert validate_plan(valid) == []
    dispatch = build_dispatch_prompts(valid)
    assert [record["id"] for record in dispatch] == ["E1", "E2", "E3"], dispatch
    assert "Acceptance:" in dispatch[0]["prompt"], dispatch[0]["prompt"]

    duplicate = json.loads(json.dumps(valid))
    duplicate["subtasks"][1]["id"] = "E1"
    assert any("duplicates" in err for err in validate_plan(duplicate))

    bad_lane = json.loads(json.dumps(valid))
    bad_lane["subtasks"][0]["lane"] = "invalid"
    assert any(".lane must be one of" in err for err in validate_plan(bad_lane))

    bad_dep = json.loads(json.dumps(valid))
    bad_dep["subtasks"][1]["dependencies"] = ["E999"]
    assert any("references unknown subtask ID 'E999'" in err for err in validate_plan(bad_dep))

    bad_order = json.loads(json.dumps(valid))
    bad_order["integration"]["order"] = ["E1", "E3"]
    assert any("missing subtask IDs" in err for err in validate_plan(bad_order))

    empty_checks = json.loads(json.dumps(valid))
    empty_checks["subtasks"][0]["acceptance"] = []
    empty_checks["subtasks"][1]["validation"] = [""]
    empty_checks["integration"]["final_verification"] = []
    errs = validate_plan(empty_checks)
    assert any("subtasks[0].acceptance must be a non-empty list" in err for err in errs), errs
    assert any("subtasks[1].validation[0] must be a non-empty string" in err for err in errs), errs
    assert any(
        "integration.final_verification must be a non-empty list" in err for err in errs
    ), errs

    parsed = parse_plan_json('```json\n{"epic": {"title": "x"}}\n```')
    assert parsed == {"epic": {"title": "x"}}, parsed

    print(
        "epic_lane.py selftest: OK (prompt, schema validation, refs/order checks, dispatch prompts)"
    )


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {raw}") from exc
    if value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return value


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

        capabilities.production_heartbeat("epic-decomposition", event_type, ref="epic_lane.main")
    except Exception:
        pass


def main(argv: Sequence[str]) -> int:
    _capability_heartbeat()
    parser = argparse.ArgumentParser(
        description="Build or validate an Orchestrator epic decomposition plan."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--goal", help="large/vague goal to turn into a planner prompt")
    group.add_argument("--goal-file", help="file containing the large/vague goal")
    group.add_argument("--validate", help="plan JSON file to validate")
    group.add_argument("--selftest", action="store_true", help="run offline selftests")
    parser.add_argument("--repo", help="optional owner/repo context")
    parser.add_argument("--target", help="optional issue/PR target context")
    parser.add_argument(
        "--context", default="", help="inline context to include in the planner prompt"
    )
    parser.add_argument(
        "--context-file", help="extra context file to include in the planner prompt"
    )
    parser.add_argument("--subtask-count", type=_positive_int, help="target number of subtasks")
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    parser.add_argument(
        "--emit-dispatch-prompts",
        action="store_true",
        help="with --validate, include dispatch prompt records in JSON output",
    )
    args = parser.parse_args(list(argv))

    if args.selftest:
        _selftest()
        return 0

    if args.validate:
        try:
            plan = parse_plan_json(Path(args.validate).read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"Could not read or parse plan JSON: {exc}", file=sys.stderr)
            return 2
        errors = validate_plan(plan)
        if args.json:
            payload: dict[str, Any] = {"valid": not errors, "errors": errors}
            if not errors and args.emit_dispatch_prompts:
                payload["dispatch_prompts"] = build_dispatch_prompts(plan)
            print(json.dumps(payload, indent=2))
        elif errors:
            print("Validation FAILED:", file=sys.stderr)
            for error in errors:
                print(f"- {error}", file=sys.stderr)
        else:
            print("Validation PASSED")
        return 0 if not errors else 1

    goal = args.goal or Path(args.goal_file).read_text(encoding="utf-8")
    prompt = build_planner_prompt(
        goal=goal,
        repo=args.repo,
        target=args.target,
        context=args.context,
        context_file=args.context_file,
        subtask_count=args.subtask_count,
    )
    if args.json:
        print(json.dumps({"prompt": prompt}, indent=2))
    else:
        print(prompt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
