#!/usr/bin/env python3
"""redirect_plan.py - safe execution plans for redirect/decompose decisions.

`redirect_policy.py` decides wait/collect/inspect/redirect/decompose. This module
turns that decision into concrete next steps while staying read-only by default.
An explicit --apply path can run the mutating redirect/decompose steps, but only
with an exact target confirmation and a fully specified next delegate.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import capabilities

ORCH_DIR = Path(__file__).resolve().parent
PROMPT_DIR = Path.home() / ".codex" / "handoff" / "redirect-prompts"
APPLY_ACTIONS = {"redirect", "decompose"}
APPLY_STEP_IDS = {"stop-process", "release-claim", "delegate-retry", "delegate-subtasks"}


# FIVE `_slug`-SHAPED HELPERS EXIST IN THIS TREE AND THEY MUST NOT BE UNIFIED.
# `redirect_shadow._corpus_entry_slug` KEEPS `#`, `redirect_plan._prompt_path_slug` STRIPS it, and
# `exploration_backfill._exp_id_slug` uses `-` and does not map `/`->`__`. That divergence was
# filed as a hygiene item ("same target != same key across modules"), and the fix is NOT to merge
# them: verified 2026-08-21 that nothing joins their outputs -- each feeds a different identifier
# namespace (corpus entry_id / prompt file path / experiment id), and unifying would rewrite
# existing entry_ids, prompt paths and `backfill-` exp_ids, breaking dedupe against historical
# rows for no gain. The real hazard is that a shared NAME invites a future join, so each is named
# for its namespace instead. If you need a target key that crosses modules, add one deliberately;
# do not reach for whichever of these is nearest.
# The hygiene item said THREE; it is five. The other two are `claims._slug` (claim file path,
# and the only one deliberately called cross-module -- range_lane_rollout uses it to build a
# claim path, which is correct BECAUSE it is module-qualified) and `partitioned_review._slug`
# (partition_id, 48-char capped). Both are also namespace-local; neither was renamed because
# their names are already reached through their module.
def _prompt_path_slug(value: str) -> str:
    s = value.strip().lower().replace("/", "__")
    s = re.sub(r"[^a-z0-9_.-]+", "_", s)
    return s.strip("_") or "target"


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _step(
    step_id: str,
    title: str,
    detail: str,
    *,
    commands: list[list[str]] | None = None,
    mutates_state: bool = False,
    requires_confirmation: bool | None = None,
) -> dict[str, Any]:
    return {
        "id": step_id,
        "title": title,
        "detail": detail,
        "commands": commands or [],
        "mutates_state": bool(mutates_state),
        "requires_confirmation": bool(
            mutates_state if requires_confirmation is None else requires_confirmation
        ),
    }


def _policy(report: dict) -> dict:
    policy = report.get("policy_decision") or {}
    if policy:
        return policy
    return {
        "action": report.get("recommended_action") or "inspect",
        "reason": "no policy_decision present; used watch recommended_action",
        "confidence": "low",
        "advisory": True,
    }


def _monitor_commands(report: dict) -> list[list[str]]:
    cmd = ["python3", str(ORCH_DIR / "watch.py")]
    if report.get("agent"):
        cmd.extend(["--agent", str(report["agent"])])
    if report.get("target"):
        cmd.extend(["--target", str(report["target"])])
    if report.get("lane"):
        cmd.extend(["--lane", str(report["lane"])])
    if report.get("pid") is not None:
        cmd.extend(["--pid", str(report["pid"])])
    if report.get("log"):
        cmd.extend(["--log", str(report["log"])])
    if report.get("worktree"):
        cmd.extend(["--worktree", str(report["worktree"])])
    if report.get("base_ref"):
        cmd.extend(["--base-ref", str(report["base_ref"])])
    for expected in _as_list(report.get("expected_paths")):
        cmd.extend(["--expected-path", str(expected)])
    cmd.append("--json")
    return [cmd]


def _inspect_commands(report: dict) -> list[list[str]]:
    commands: list[list[str]] = []
    log = report.get("log")
    worktree = report.get("worktree")
    base_ref = report.get("base_ref")
    if log:
        commands.append(["tail", "-n", "80", str(log)])
    if worktree:
        commands.append(["git", "-C", str(worktree), "status", "--short"])
        if base_ref:
            commands.append(["git", "-C", str(worktree), "diff", "--stat", str(base_ref)])
            commands.append(["git", "-C", str(worktree), "diff", "--name-only", str(base_ref)])
        else:
            commands.append(["git", "-C", str(worktree), "diff", "--stat"])
            commands.append(["git", "-C", str(worktree), "diff", "--name-only"])
    return commands


def _drift_summary(report: dict) -> list[str]:
    drift = report.get("drift") or {}
    lines = []
    if drift.get("severity"):
        lines.append(f"Drift severity: {drift.get('severity')}")
    for finding in drift.get("findings") or []:
        kind = finding.get("kind") or "finding"
        detail = finding.get("detail") or ""
        paths = ", ".join((finding.get("paths") or [])[:5])
        suffix = f" Paths: {paths}" if paths else ""
        lines.append(f"- {kind}: {detail}{suffix}")
    return lines


def _hint_summary(report: dict) -> list[str]:
    out = []
    for hint in report.get("hints") or []:
        out.append(f"- {hint.get('kind')}: {hint.get('detail')}")
    return out


def _prompt_text(report: dict, action: str, reason: str) -> str:
    target = report.get("target") or "<target>"
    agent = report.get("agent") or "<previous-agent>"
    state = report.get("state") or "<unknown>"
    expected = (
        ", ".join(str(p) for p in _as_list(report.get("expected_paths"))) or "(not specified)"
    )
    lines = [
        f"Take over {target} after a prior {agent} attempt was classified as {state}.",
        "",
        f"Redirect action: {action}",
        f"Reason: {reason}",
        f"Expected path scope: {expected}",
        "",
        "Use the normal Orchestrator delegate workflow: inspect the current task context, keep the diff",
        "strictly in scope, satisfy the acceptance criteria, run focused validation, commit, push, and",
        "open/update the PR as appropriate for this lane.",
    ]
    hints = _hint_summary(report)
    if hints:
        lines.extend(["", "Root-cause hints from the prior log:", *hints])
    drift = _drift_summary(report)
    if drift:
        lines.extend(["", "Drift signals to avoid repeating:", *drift])
    tail = (report.get("log_tail") or "").strip()
    if tail:
        lines.extend(["", "Prior log tail:", tail[-1000:]])
    if action == "decompose":
        lines.extend(
            [
                "",
                "Before coding, split the work into 2-3 independently verifiable slices. Start with the",
                "smallest slice that can produce a real acceptance-criteria signal, and report the remaining",
                "slices explicitly so the orchestrator can assign them separately if useful.",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def _prompt_path(report: dict, action: str, prompt_file: str | None) -> str:
    if prompt_file:
        return prompt_file
    target = report.get("target") or "target"
    agent = report.get("agent") or "agent"
    return str(PROMPT_DIR / f"{_prompt_path_slug(target)}.{_prompt_path_slug(agent)}.{action}.md")


def _delegate_commands(
    report: dict,
    *,
    next_agent: str | None,
    lane: str | None,
    task_type: str | None,
    prompt_file: str | None,
) -> list[list[str]]:
    target = report.get("target") or "<target>"
    selected_agent = next_agent or "<next-agent>"
    selected_lane = lane or report.get("lane") or "<lane>"
    selected_type = task_type or report.get("task_type") or "implement"
    return [
        [
            "python3",
            str(ORCH_DIR / "dispatcher.py"),
            "delegate",
            "--agent",
            selected_agent,
            "--target",
            str(target),
            "--lane",
            str(selected_lane),
            "--task-type",
            str(selected_type),
            "--prompt-file",
            str(prompt_file),
        ]
    ]


def _common_inspection_step(report: dict) -> dict[str, Any]:
    return _step(
        "inspect-current-state",
        "Inspect current state",
        "Review the log tail and changed paths before accepting, redirecting, or decomposing the lane.",
        commands=_inspect_commands(report),
    )


def plan(
    report: dict,
    *,
    next_agent: str | None = None,
    lane: str | None = None,
    task_type: str | None = None,
    prompt_file: str | None = None,
    prompt_override: str | None = None,
) -> dict[str, Any]:
    """Return a dry-run plan for a watch report. This function has no side effects.

    prompt_override (optional): when an agent-role (see roles.py RedirectAgent) has authored a
    corrected delegation prompt, pass it here; it replaces the deterministic _prompt_text for
    redirect/decompose actions. None preserves the prior template behavior.
    """
    # Credit at the function the drivers actually call. The heartbeat sat only in apply_plan(),
    # which the shadow sweep never reaches -- it PROPOSES and never applies -- so 143 role:redirect
    # runs recorded nothing for this capability. This module calls production_heartbeat directly
    # (it imports `capabilities` at module scope) rather than via the _capability_heartbeat helper
    # the sibling modules use; matching the local idiom avoids a NameError. (2026-08-20)
    try:
        capabilities.production_heartbeat("redirect-plan", "invocation", ref="redirect_plan.plan")
    except Exception:
        pass
    policy = _policy(report)
    action = str(policy.get("action") or "inspect")
    reason = str(policy.get("reason") or "")
    if action in {"redirect", "decompose"}:
        prompt = prompt_override if prompt_override else _prompt_text(report, action, reason)
    else:
        prompt = ""
    prompt_path = _prompt_path(report, action, prompt_file) if prompt else ""
    steps: list[dict[str, Any]] = []

    if action == "wait":
        steps.append(
            _step(
                "wait-and-recheck",
                "Wait and recheck",
                "Lane is active without drift/root-cause signals; re-run watch after the stale interval.",
                commands=_monitor_commands(report),
            )
        )
    elif action == "collect":
        steps.append(
            _step(
                "collect-result",
                "Collect produced work",
                "Agent appears done or idle after producing changes; inspect the diff and validation notes.",
                commands=_inspect_commands(report),
            )
        )
    elif action == "inspect":
        steps.append(_common_inspection_step(report))
    elif action in {"redirect", "decompose"}:
        steps.append(_common_inspection_step(report))
        pid = report.get("pid")
        if pid is not None:
            steps.append(
                _step(
                    "stop-process",
                    "Stop current process",
                    "Only stop the lane after confirming it is still the stale/drifting process from this report.",
                    commands=[["kill", str(pid)]],
                    mutates_state=True,
                )
            )
        if report.get("target"):
            release_cmd = ["python3", str(ORCH_DIR / "claims.py"), "release", str(report["target"])]
            if report.get("agent"):
                release_cmd.append(str(report["agent"]))
            steps.append(
                _step(
                    "release-claim",
                    "Release target claim",
                    "Release the claim only after the current process is stopped or confirmed exited.",
                    commands=[release_cmd],
                    mutates_state=True,
                )
            )
        if action == "redirect":
            steps.append(
                _step(
                    "delegate-retry",
                    "Delegate retry",
                    "Write prompt_text to prompt_file, choose a suitable next agent, then delegate the retry.",
                    commands=_delegate_commands(
                        report,
                        next_agent=next_agent,
                        lane=lane,
                        task_type=task_type,
                        prompt_file=prompt_path,
                    ),
                    mutates_state=True,
                )
            )
        else:
            steps.append(
                _step(
                    "delegate-subtasks",
                    "Delegate decomposed slice",
                    "Use prompt_text as the decomposition brief; delegate the first independently verifiable slice.",
                    commands=_delegate_commands(
                        report,
                        next_agent=next_agent,
                        lane=lane,
                        task_type=task_type,
                        prompt_file=prompt_path,
                    ),
                    mutates_state=True,
                )
            )
    else:
        steps.append(
            _step(
                "inspect-current-state",
                "Inspect current state",
                f"Unknown policy action {action!r}; fall back to manual inspection.",
                commands=_inspect_commands(report),
            )
        )

    mutates = any(step["mutates_state"] for step in steps)
    requires = any(step["requires_confirmation"] for step in steps)
    return {
        "version": 2,
        "advisory": True,
        "dry_run_only": True,
        "apply_supported": action in APPLY_ACTIONS,
        "action": action,
        "target": report.get("target") or "",
        "agent": report.get("agent") or "",
        "lane": lane or report.get("lane") or "",
        "task_type": task_type or report.get("task_type") or "",
        "reason": reason,
        "confidence": policy.get("confidence") or "",
        "mutates_state": mutates,
        "requires_confirmation": requires,
        "prompt_file": prompt_path,
        "prompt_text": prompt,
        "steps": steps,
    }


def format_human(plan_obj: dict[str, Any]) -> str:
    lines = [
        f"action={plan_obj.get('action')} target={plan_obj.get('target') or '-'} "
        f"agent={plan_obj.get('agent') or '-'} confidence={plan_obj.get('confidence') or '-'}",
        f"reason={plan_obj.get('reason') or '-'}",
        f"dry_run_only={plan_obj.get('dry_run_only')} requires_confirmation={plan_obj.get('requires_confirmation')}",
    ]
    if plan_obj.get("prompt_file"):
        lines.append(f"prompt_file={plan_obj['prompt_file']}")
    for idx, step in enumerate(plan_obj.get("steps") or [], start=1):
        flag = " CONFIRM" if step.get("requires_confirmation") else ""
        lines.append(f"{idx}. {step.get('title')}{flag}: {step.get('detail')}")
        for command in step.get("commands") or []:
            lines.append("   $ " + shlex.join(command))
    if plan_obj.get("prompt_text"):
        lines.append("--- prompt_text ---")
        lines.append(plan_obj["prompt_text"].rstrip())
    return "\n".join(lines)


def attach_role_lineage(plan_obj: dict[str, Any], role_run_id: str) -> dict[str, Any]:
    """Carry an accepted RedirectAgent run into the eventual downstream dispatch."""
    if not role_run_id:
        raise ValueError("role_run_id is required")
    out = {**plan_obj, "accepted_role_run_id": role_run_id}
    out["steps"] = []
    for step in plan_obj.get("steps") or []:
        copied = {**step, "commands": [list(command) for command in step.get("commands") or []]}
        if copied.get("id") in {"delegate-retry", "delegate-subtasks"}:
            for command in copied["commands"]:
                command.extend(["--influenced-by-role-run-id", role_run_id])
        out["steps"].append(copied)
    return out


def _has_placeholder(command: list[str]) -> bool:
    return any(part.startswith("<") and part.endswith(">") for part in command)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _command_result(
    proc: subprocess.CompletedProcess[str], *, step_id: str, command: list[str]
) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "command": command,
        "returncode": proc.returncode,
        "stdout_tail": (proc.stdout or "")[-800:],
        "stderr_tail": (proc.stderr or "")[-800:],
    }


def apply_plan(
    plan_obj: dict[str, Any],
    *,
    confirm_target: str,
    runner=subprocess.run,
    pid_checker=_pid_alive,
) -> dict[str, Any]:
    """Apply only the guarded mutating portion of a redirect/decompose plan.

    The caller must pass the exact target in `confirm_target`. This writes the
    generated prompt before stopping anything, skips kill when the PID is already
    gone, and runs only the mutating stop/release/delegate steps. Read-only
    inspection commands remain advisory. A missing/already-released claim is
    non-fatal; delegate failure still aborts.
    """
    action = plan_obj.get("action")
    target = plan_obj.get("target") or ""
    if action not in APPLY_ACTIONS:
        raise ValueError(f"apply is only supported for redirect/decompose plans, got {action!r}")
    if not target or confirm_target != target:
        raise ValueError("apply requires --confirm-target exactly matching the plan target")
    if not plan_obj.get("prompt_text") or not plan_obj.get("prompt_file"):
        raise ValueError("apply requires prompt_text and prompt_file in the plan")

    capabilities.production_heartbeat(
        "redirect-plan",
        "match",
        ref=target,
        metadata={"action": action},
    )

    mutating_steps = [s for s in plan_obj.get("steps") or [] if s.get("id") in APPLY_STEP_IDS]
    commands = [cmd for step in mutating_steps for cmd in step.get("commands") or []]
    placeholder_cmds = [cmd for cmd in commands if _has_placeholder(cmd)]
    if placeholder_cmds:
        rendered = "; ".join(shlex.join(cmd) for cmd in placeholder_cmds)
        raise ValueError(f"apply refused placeholder command(s): {rendered}")

    prompt_path = Path(str(plan_obj["prompt_file"])).expanduser()
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(str(plan_obj["prompt_text"]))
    capabilities.production_heartbeat(
        "redirect-plan",
        "invocation",
        ref=str(prompt_path),
        metadata={"target": target, "action": action},
    )

    result: dict[str, Any] = {
        "applied": False,
        "target": target,
        "action": action,
        "prompt_written": str(prompt_path),
        "executed": [],
        "skipped": [],
    }

    for step in mutating_steps:
        step_id = step.get("id") or ""
        for command in step.get("commands") or []:
            if step_id == "stop-process":
                try:
                    pid = int(command[1])
                except (IndexError, TypeError, ValueError):
                    raise ValueError(f"invalid stop-process command: {command!r}") from None
                if not pid_checker(pid):
                    result["skipped"].append(
                        {
                            "step_id": step_id,
                            "command": command,
                            "reason": "pid is not alive",
                        }
                    )
                    continue

            proc = runner(command, capture_output=True, text=True, check=False)
            item = _command_result(proc, step_id=step_id, command=command)
            result["executed"].append(item)
            if proc.returncode != 0 and step_id != "release-claim":
                result["aborted_at"] = step_id
                result["applied"] = False
                return result

    result["applied"] = not any(
        item["returncode"] != 0 and item["step_id"] != "release-claim"
        for item in result["executed"]
    )
    if result["applied"]:
        capabilities.production_heartbeat(
            "redirect-plan",
            "success",
            ref=str(prompt_path),
            metadata={"target": target, "action": action},
        )
    return result


def _selftest() -> None:
    base = {
        "agent": "cursor",
        "target": "stranske/Repo#12",
        "lane": "opener",
        "task_type": "implement",
        "pid": 12345,
        "log": "/tmp/agent.log",
        "worktree": "/tmp/worktree",
        "base_ref": "origin/main",
        "expected_paths": ["src"],
        "state": "stalled",
        "recommended_action": "inspect",
        "signals": {"pid_alive": True, "has_worktree_changes": True},
        "hints": [{"kind": "auth", "detail": "401 Unauthorized"}],
        "drift": {"severity": "none", "findings": []},
        "log_tail": "HTTP 401 Unauthorized\n",
    }

    wait_report = {
        **base,
        "state": "progress",
        "policy_decision": {"action": "wait", "reason": "active", "confidence": "high"},
    }
    wait = plan(wait_report)
    assert wait["action"] == "wait" and not wait["mutates_state"], wait
    assert wait["steps"][0]["commands"][0][0:2] == ["python3", str(ORCH_DIR / "watch.py")], wait

    collect_report = {
        **base,
        "policy_decision": {"action": "collect", "reason": "done", "confidence": "high"},
    }
    collect = plan(collect_report)
    assert collect["action"] == "collect" and not collect["requires_confirmation"], collect
    assert any(
        cmd[:3] == ["git", "-C", "/tmp/worktree"] for cmd in collect["steps"][0]["commands"]
    ), collect

    redirect_report = {
        **base,
        "policy_decision": {
            "action": "redirect",
            "reason": "auth root cause",
            "confidence": "high",
        },
    }
    redirect = plan(redirect_report, next_agent="vibe")
    assert redirect["requires_confirmation"] and redirect["mutates_state"], redirect
    ids = [step["id"] for step in redirect["steps"]]
    assert "stop-process" in ids and "release-claim" in ids and "delegate-retry" in ids, redirect
    assert any(
        cmd == ["kill", "12345"] for step in redirect["steps"] for cmd in step["commands"]
    ), redirect
    assert (
        "auth root cause" in redirect["prompt_text"]
        and "401 Unauthorized" in redirect["prompt_text"]
    ), redirect
    assert (
        "--agent" in redirect["steps"][-1]["commands"][0]
        and "vibe" in redirect["steps"][-1]["commands"][0]
    ), redirect

    decompose_report = {
        **base,
        "policy_decision": {
            "action": "decompose",
            "reason": "repeated stalls",
            "confidence": "medium",
        },
        "drift": {
            "severity": "medium",
            "findings": [
                {"kind": "unexpected_paths", "detail": "outside scope", "paths": ["infra.yml"]}
            ],
        },
    }
    decompose = plan(decompose_report, prompt_file="/tmp/decompose.md")
    assert (
        decompose["action"] == "decompose" and decompose["prompt_file"] == "/tmp/decompose.md"
    ), decompose
    assert "split the work" in decompose["prompt_text"].lower(), decompose["prompt_text"]
    assert "unexpected_paths" in decompose["prompt_text"], decompose["prompt_text"]

    # An agent-authored prompt (roles.py RedirectAgent) replaces the template for redirect/decompose,
    # and is ignored for non-mutating actions.
    override = plan(redirect_report, next_agent="vibe", prompt_override="AGENT CORRECTED PROMPT")
    assert override["prompt_text"] == "AGENT CORRECTED PROMPT", override
    assert (
        plan(wait_report, prompt_override="ignored").get("prompt_text") == ""
    ), "override must not apply to wait"

    import tempfile

    with tempfile.TemporaryDirectory(prefix="redirect-plan-selftest-") as tmp:
        prompt_file = str(Path(tmp) / "retry prompt.md")
        applied_plan = plan(redirect_report, next_agent="vibe", prompt_file=prompt_file)
        calls: list[list[str]] = []

        def fake_runner(command, **_kwargs):
            calls.append(command)
            rc = 1 if "claims.py" in command[1] else 0
            stdout = '{"released": false}' if rc else '{"ok": true}'
            return subprocess.CompletedProcess(command, rc, stdout, "")

        applied = apply_plan(
            applied_plan,
            confirm_target="stranske/Repo#12",
            runner=fake_runner,
            pid_checker=lambda _pid: True,
        )
        assert applied["applied"] is True, applied
        assert Path(prompt_file).read_text() == applied_plan["prompt_text"], applied
        assert calls[0] == ["kill", "12345"], calls
        assert any("claims.py" in cmd[1] for cmd in calls), calls
        assert calls[-1][calls[-1].index("--agent") + 1] == "vibe", calls[-1]

        no_pid = apply_plan(
            applied_plan,
            confirm_target="stranske/Repo#12",
            runner=fake_runner,
            pid_checker=lambda _pid: False,
        )
        assert any(item["step_id"] == "stop-process" for item in no_pid["skipped"]), no_pid

        try:
            apply_plan(applied_plan, confirm_target="wrong/Target#1", runner=fake_runner)
            raise AssertionError("wrong confirm target should fail")
        except ValueError as exc:
            assert "confirm-target" in str(exc), exc

        placeholder = plan(redirect_report, prompt_file=prompt_file)
        try:
            apply_plan(placeholder, confirm_target="stranske/Repo#12", runner=fake_runner)
            raise AssertionError("placeholder next-agent should fail")
        except ValueError as exc:
            assert "placeholder" in str(exc), exc

    print("redirect_plan.py selftest: OK (dry-run plans + guarded apply)")


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        _selftest()
        return 0
    parser = argparse.ArgumentParser(
        description="Build a safe dry-run redirect/decompose plan from a watch report."
    )
    parser.add_argument(
        "--report-json", default="", help="watch.py JSON report; stdin is used when omitted"
    )
    parser.add_argument("--next-agent", default="")
    parser.add_argument("--lane", default="")
    parser.add_argument("--task-type", default="")
    parser.add_argument("--prompt-file", default="")
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "execute guarded mutating redirect/decompose steps; requires "
            "--confirm-target and --next-agent; writes prompt first, refuses "
            "placeholders, skips dead PIDs, and aborts on delegate failure"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="default behavior: print the plan without executing mutating steps",
    )
    parser.add_argument(
        "--confirm-target",
        default="",
        help="required with --apply; must exactly match the plan target",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    if args.apply and not args.confirm_target:
        parser.error("--apply requires --confirm-target <exact-target>")
    if args.apply and not args.next_agent:
        parser.error("--apply requires --next-agent <agent>")

    if args.report_json:
        report = json.loads(Path(args.report_json).read_text())
    else:
        report = json.load(sys.stdin)
    out = plan(
        report,
        next_agent=args.next_agent or None,
        lane=args.lane or None,
        task_type=args.task_type or None,
        prompt_file=args.prompt_file or None,
    )
    if args.apply:
        try:
            applied = apply_plan(out, confirm_target=args.confirm_target)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(
            json.dumps(applied, indent=2)
            if args.as_json
            else format_human(out) + "\n--- apply_result ---\n" + json.dumps(applied, indent=2)
        )
        return 0 if applied.get("applied") else 1
    if args.as_json:
        print(json.dumps(out, indent=2))
    else:
        print(format_human(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
