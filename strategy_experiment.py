#!/usr/bin/env python3
"""strategy_experiment.py - guarded H4/H5 multi-agent strategy experiment surface.

The research scheduler can already see strategy arms such as "single claude" vs
"parallel claude+cursor with synthesis", but the autonomous tick must keep
launching only simple one-worktree-per-agent A/B jobs. This module supplies the
missing manual/supervised bridge: normalize strategy arms, expand them to the
implementation agents that `exp_abcd.py` can run, write durable strategy
metadata, and optionally call `exp_abcd.prepare` behind an explicit active
guard.

Default behavior is read-only planning. Active prepare requires:

    ORCH_STRATEGY_EXPERIMENT=1 python3 strategy_experiment.py ... --prepare --confirm-strategy

Pure helpers are selftested offline with `--selftest`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Sequence

import exp_abcd
import research_scheduler

ORCH = Path(__file__).resolve().parent
DEFAULT_TASK_TYPE = "implement"
SUPPORTED_STRATEGIES = {"single", "parallel"}


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "arm"


def _dedupe_preserve(values: Sequence[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        if value not in out:
            out.append(value)
    return out


def _coerce_agents(raw: Any) -> list[str]:
    if isinstance(raw, str):
        agents = [raw]
    elif isinstance(raw, list):
        agents = raw
    else:
        raise ValueError(f"invalid agents value: {raw!r}")
    out = [" ".join(str(agent).strip().split()) for agent in agents]
    out = [agent for agent in out if agent]
    if not out:
        raise ValueError("strategy arm must name at least one agent")
    if len(out) != len(set(out)):
        raise ValueError(f"duplicate agent inside strategy arm: {out!r}")
    return out


def normalize_arm(raw: Any, index: int = 0) -> dict[str, Any]:
    """Normalize a single-agent or strategy arm into durable metadata."""
    if isinstance(raw, str):
        agents = _coerce_agents(raw)
        strategy = "single"
        synthesize = False
    elif isinstance(raw, dict):
        if "agents" in raw:
            agents = _coerce_agents(raw.get("agents"))
            strategy = str(raw.get("strategy") or "").strip().lower()
        elif "parallel" in raw:
            agents = _coerce_agents(raw.get("parallel"))
            strategy = "parallel"
        elif "single" in raw:
            agents = _coerce_agents(raw.get("single"))
            strategy = "single"
        else:
            raise ValueError(f"strategy arm lacks agents: {raw!r}")
        if not strategy:
            strategy = "single" if len(agents) == 1 else "parallel"
        synthesize = bool(raw.get("synthesize"))
    else:
        raise ValueError(f"invalid strategy arm: {raw!r}")

    if strategy not in SUPPORTED_STRATEGIES:
        raise ValueError(
            f"unsupported strategy {strategy!r}; expected one of {sorted(SUPPORTED_STRATEGIES)}"
        )
    if strategy == "single" and len(agents) != 1:
        raise ValueError(f"single strategy must name exactly one agent: {agents!r}")
    if strategy == "parallel" and len(agents) < 2:
        raise ValueError(f"parallel strategy must name at least two agents: {agents!r}")

    label = f"{strategy}({'+'.join(agents)}{'+synth' if synthesize else ''})"
    arm_id = f"arm-{index + 1:02d}-{_slug(label)}"
    arm_profile_id = raw.get("profile_id") if isinstance(raw, dict) else None
    raw_member_profiles = raw.get("member_profiles") if isinstance(raw, dict) else None
    members = [
        {
            "arm_id": arm_id,
            "member_id": exp_abcd.member_identity(arm_id, agent, ordinal),
            "agent": agent,
            "profile_id": (
                raw_member_profiles.get(agent)
                if isinstance(raw_member_profiles, dict)
                else (
                    raw_member_profiles[ordinal]
                    if isinstance(raw_member_profiles, list) and ordinal < len(raw_member_profiles)
                    else arm_profile_id
                )
            ),
            "ordinal": ordinal,
        }
        for ordinal, agent in enumerate(agents)
    ]
    return {
        "arm_id": arm_id,
        "strategy": strategy,
        "agents": agents,
        "members": members,
        "synthesize": synthesize,
        "label": label,
        "profile_id": arm_profile_id,
        "cost_basis": ("sum_agent_runs" if strategy == "parallel" else "single_agent_run"),
    }


def normalize_arms(arms: Sequence[Any]) -> list[dict[str, Any]]:
    normalized = [normalize_arm(arm, i) for i, arm in enumerate(arms)]
    if len(normalized) < 2:
        raise ValueError("strategy experiment needs at least two arms")
    return normalized


def implementation_agents(arms: Sequence[Any]) -> list[str]:
    normalized = (
        arms if arms and isinstance(arms[0], dict) and "arm_id" in arms[0] else normalize_arms(arms)
    )
    return _dedupe_preserve(agent for arm in normalized for agent in arm["agents"])


def load_hypothesis_arms(
    hypothesis_id: str, path: Path | None = None
) -> tuple[dict[str, Any], list[Any]]:
    hyps = research_scheduler.load_hypotheses(path or research_scheduler.HYP_PATH)
    for hyp in hyps:
        if str(hyp.get("id")) == str(hypothesis_id):
            arms = hyp.get("arms") or []
            if not isinstance(arms, list):
                raise ValueError(f"hypothesis {hypothesis_id} arms must be a list")
            return hyp, arms
    raise ValueError(f"hypothesis {hypothesis_id!r} not found")


def read_arms_json(value: str) -> list[Any]:
    stripped = value.lstrip()
    if stripped.startswith(("[", "{")):
        text = value
    else:
        path = Path(value).expanduser()
        text = path.read_text() if path.exists() else value
    parsed = json.loads(text)
    if isinstance(parsed, dict):
        parsed = parsed.get("arms")
    if not isinstance(parsed, list):
        raise ValueError("--arms-json must be a JSON list or an object with an arms list")
    return parsed


def strategy_metadata_path(exp_id: str, exp_dir: Path | None = None) -> Path:
    return (exp_dir or exp_abcd.EXP_DIR) / exp_id / "strategy.json"


def _command_text(argv: Sequence[str], env: dict[str, str] | None = None) -> str:
    prefix = ""
    if env:
        prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items()) + " "
    return prefix + shlex.join(list(argv))


def build_strategy_plan(
    repo: str,
    spec_file: str,
    exp_id: str,
    arms: Sequence[Any],
    *,
    hypothesis: str | None = None,
    task_type: str = DEFAULT_TASK_TYPE,
    exp_dir: Path | None = None,
    python: str = "python3",
) -> dict[str, Any]:
    normalized = normalize_arms(arms)
    agents = implementation_agents(normalized)
    for arm in normalized:
        arm["member_run_ids"] = [
            f"{exp_id}:member:{member['member_id']}" for member in arm["members"]
        ]
        arm["synthesis_requested"] = bool(arm.get("synthesize"))
        arm["synthesis_run_id"] = (
            f"{exp_id}:synth:{arm['arm_id']}" if arm["synthesis_requested"] else None
        )
        arm["attempt_run_ids"] = [
            *arm["member_run_ids"],
            *([arm["synthesis_run_id"]] if arm["synthesis_run_id"] else []),
        ]
        arm["planned_attempt_count"] = len(arm["attempt_run_ids"])
        arm["final_artifact_id"] = (
            f"{exp_id}:artifact:{arm['arm_id']}:synth"
            if arm["synthesis_requested"]
            else (
                arm["members"][0]["member_id"]
                if len(arm["members"]) == 1
                else f"{exp_id}:artifact:{arm['arm_id']}:member-set"
            )
        )
    strategy_args: list[str] = [
        python,
        str(ORCH / "strategy_experiment.py"),
        "--repo",
        repo,
        "--spec-file",
        spec_file,
        "--exp-id",
        exp_id,
    ]
    if hypothesis:
        strategy_args.extend(["--hypothesis", str(hypothesis)])
    else:
        strategy_args.extend(["--arms-json", json.dumps(list(arms), separators=(",", ":"))])
    if task_type:
        strategy_args.extend(["--task-type", task_type])

    active_prepare_args = [*strategy_args, "--prepare", "--confirm-strategy"]
    # Tranche 0 lane B: Use arm-aware prepare-arms command
    arms_json = json.dumps(normalized, separators=(",", ":"))
    exp_abcd_prepare = [
        python,
        str(ORCH / "exp_abcd.py"),
        "prepare-arms",
        repo,
        spec_file,
        exp_id,
        arms_json,
    ]
    status = [python, str(ORCH / "exp_abcd.py"), "status", exp_id]
    collect = [python, str(ORCH / "exp_abcd.py"), "collect", repo, exp_id]
    evaluate = [python, str(ORCH / "exp_abcd.py"), "evaluate", repo, spec_file, exp_id]
    synthesize = [python, str(ORCH / "exp_abcd.py"), "synthesize", repo, exp_id]

    return {
        "kind": "strategy_experiment_plan",
        "strategy_aware_auto_launch": False,
        "repo": repo,
        "spec_file": spec_file,
        "exp_id": exp_id,
        "task_type": task_type or DEFAULT_TASK_TYPE,
        "hypothesis": hypothesis,
        "arms": normalized,
        "implementation_agents": agents,
        "metadata_path": str(strategy_metadata_path(exp_id, exp_dir=exp_dir)),
        "commands": {
            "plan": [*strategy_args, "--json"],
            "active_prepare": active_prepare_args,
            "active_prepare_text": _command_text(
                active_prepare_args,
                env={"ORCH_STRATEGY_EXPERIMENT": "1"},
            ),
            "exp_abcd_prepare": exp_abcd_prepare,
            "status": status,
            "collect": collect,
            "evaluate": evaluate,
            "synthesize": synthesize,
        },
        "active_prepare_guard": {
            "requires_prepare_flag": True,
            "requires_confirm_strategy": True,
            "requires_env": {"ORCH_STRATEGY_EXPERIMENT": "1"},
        },
        "scoring_contract": {
            "unit": "strategy_arm",
            "quality_source": "cross-eval scores plus optional synthesized diff for synthesize=true arms",
            "cost_source": "sum member implementation runs plus synthesis run when present",
            "feedback_followup": "record strategy outcomes via decomposition metadata after collect/evaluate/synthesize",
        },
    }


def strategy_metadata(
    plan: dict[str, Any], *, prepared: dict[str, Any] | None = None
) -> dict[str, Any]:
    metadata = {
        "schema_version": 2,
        "kind": "strategy_experiment",
        "created_ts": int(time.time()),
        "strategy_aware_auto_launch": False,
        "repo": plan["repo"],
        "spec_file": plan["spec_file"],
        "exp_id": plan["exp_id"],
        "task_type": plan["task_type"],
        "hypothesis": plan.get("hypothesis"),
        "strategy_arms": plan["arms"],
        "implementation_agents": plan["implementation_agents"],
        "commands": plan["commands"],
        "scoring_contract": plan["scoring_contract"],
    }
    if prepared is not None:
        metadata["prepared"] = prepared
        metadata["prepared_ts"] = int(time.time())
    return metadata


def prepare_strategy_experiment(
    plan: dict[str, Any],
    *,
    prepare_fn: Callable[
        [str, str, str, list[dict[str, Any]]], dict[str, Any]
    ] = exp_abcd.prepare_arms,
) -> dict[str, Any]:
    """Prepare strategy arms without flattening shared members across arms."""
    metadata_path = Path(plan["metadata_path"])
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(strategy_metadata(plan), indent=2, sort_keys=True))

    arms = plan["arms"]
    prepared = prepare_fn(
        plan["repo"],
        plan["spec_file"],
        plan["exp_id"],
        arms,
    )

    metadata_path.write_text(
        json.dumps(strategy_metadata(plan, prepared=prepared), indent=2, sort_keys=True)
    )
    return {
        **plan,
        "prepared": prepared,
        "metadata_written": str(metadata_path),
    }


def strategy_arm_costs(
    plan: dict[str, Any], cost_rows: Sequence[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Sum observed member and synthesis attempts for each strategy arm."""
    by_run = {str(row.get("run_id")): row for row in cost_rows if row.get("run_id")}
    out: dict[str, dict[str, Any]] = {}
    for arm in plan["arms"]:
        attempt_ids = list(arm.get("attempt_run_ids") or [])
        observed = [by_run[run_id] for run_id in attempt_ids if run_id in by_run]
        out[arm["arm_id"]] = {
            "attempt_run_ids": attempt_ids,
            "planned_attempt_count": len(attempt_ids),
            "observed_attempt_count": len(observed),
            "missing_attempt_run_ids": [run_id for run_id in attempt_ids if run_id not in by_run],
            "tokens_in": sum(int(row.get("tokens_in") or 0) for row in observed),
            "tokens_out": sum(int(row.get("tokens_out") or 0) for row in observed),
            "cost_usd": round(sum(float(row.get("cost_usd") or 0.0) for row in observed), 8),
            "latency_s": sum(float(row.get("latency_s") or 0.0) for row in observed),
            "final_artifact_id": arm.get("final_artifact_id"),
        }
    return out


def _format_plan(plan: dict[str, Any]) -> str:
    lines = [
        f"Strategy experiment plan: {plan['exp_id']}",
        f"repo: {plan['repo']}",
        f"task_type: {plan['task_type']}",
    ]
    if plan.get("hypothesis"):
        lines.append(f"hypothesis: {plan['hypothesis']}")
    lines.append("arms:")
    for arm in plan["arms"]:
        lines.append(
            f"- {arm['arm_id']}: {arm['label']} "
            f"(agents={','.join(arm['agents'])}, cost={arm['cost_basis']})"
        )
    lines.extend(
        [
            f"implementation agents: {','.join(plan['implementation_agents'])}",
            f"metadata: {plan['metadata_path']}",
            "active prepare:",
            plan["commands"]["active_prepare_text"],
            "follow-up:",
            _command_text(plan["commands"]["status"]),
            _command_text(plan["commands"]["collect"]),
            _command_text(plan["commands"]["evaluate"]),
            _command_text(plan["commands"]["synthesize"]),
        ]
    )
    return "\n".join(lines)


def _build_plan_from_args(args: argparse.Namespace) -> dict[str, Any]:
    hypothesis = None
    task_type = args.task_type or DEFAULT_TASK_TYPE
    if args.hypothesis:
        hyp, arms = load_hypothesis_arms(
            args.hypothesis,
            Path(args.hypotheses_path).expanduser() if args.hypotheses_path else None,
        )
        hypothesis = str(hyp.get("id"))
        task_type = args.task_type or str(hyp.get("task_type") or DEFAULT_TASK_TYPE)
    else:
        arms = read_arms_json(args.arms_json)
    return build_strategy_plan(
        args.repo,
        args.spec_file,
        args.exp_id,
        arms,
        hypothesis=hypothesis,
        task_type=task_type,
    )


def _selftest() -> None:
    para = {"strategy": "parallel", "agents": ["claude", "cursor"], "synthesize": True}
    single = {"strategy": "single", "agents": ["claude"]}
    n_single = normalize_arm(single, 0)
    n_para = normalize_arm(para, 1)
    assert n_single["label"] == "single(claude)", n_single
    assert n_para["label"] == "parallel(claude+cursor+synth)", n_para
    assert implementation_agents([single, para]) == ["claude", "cursor"]
    assert (
        normalize_arm({"parallel": ["codex", "cursor"], "synthesize": True})["strategy"]
        == "parallel"
    )
    try:
        normalize_arm({"strategy": "single", "agents": ["claude", "cursor"]})
        assert False, "invalid single arm should fail"
    except ValueError as exc:
        assert "single strategy" in str(exc), exc

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        spec = tmp / "spec.md"
        spec.write_text("frozen spec")
        hyp_path = tmp / "hypotheses.json"
        hyp, h4_arms = load_hypothesis_arms("H4", hyp_path)
        assert hyp["id"] == "H4" and len(h4_arms) == 2, hyp
        parsed_arms = read_arms_json(json.dumps({"arms": h4_arms}))
        assert parsed_arms == h4_arms
        plan = build_strategy_plan(
            "stranske/Workflows",
            str(spec),
            "strategy-selftest",
            h4_arms,
            hypothesis="H4",
            exp_dir=tmp / "experiments",
        )
        assert plan["strategy_aware_auto_launch"] is False, plan
        assert plan["implementation_agents"] == ["claude", "cursor"], plan
        assert plan["arms"][1]["member_run_ids"] == [
            "strategy-selftest:member:arm-02-parallel-claude-cursor-synth--member-01-claude",
            "strategy-selftest:member:arm-02-parallel-claude-cursor-synth--member-02-cursor",
        ], plan
        assert plan["arms"][1]["planned_attempt_count"] == 3, plan["arms"][1]
        assert "ORCH_STRATEGY_EXPERIMENT=1" in plan["commands"]["active_prepare_text"]
        assert json.loads(plan["commands"]["exp_abcd_prepare"][-1]) == plan["arms"], plan

        captured: dict[str, Any] = {}

        def fake_prepare(
            repo: str, spec_file: str, exp_id: str, arms: list[dict]
        ) -> dict[str, Any]:
            captured.update(
                {
                    "repo": repo,
                    "spec_file": spec_file,
                    "exp_id": exp_id,
                    "arms": arms,
                }
            )
            return {
                "exp_id": exp_id,
                "repo": repo,
                "launched": [member for arm in arms for member in arm["members"]],
            }

        prepared = prepare_strategy_experiment(plan, prepare_fn=fake_prepare)
        assert captured["arms"] == plan["arms"], captured
        metadata_path = Path(prepared["metadata_written"])
        metadata = json.loads(metadata_path.read_text())
        assert metadata["hypothesis"] == "H4", metadata
        assert metadata["prepared"]["launched"][0]["agent"] == "claude", metadata
        costs = strategy_arm_costs(
            plan,
            [
                {
                    "run_id": run_id,
                    "tokens_in": 10,
                    "tokens_out": 5,
                    "cost_usd": 1.0,
                    "latency_s": 2.0,
                }
                for run_id in plan["arms"][1]["attempt_run_ids"]
            ],
        )
        parallel_cost = costs[plan["arms"][1]["arm_id"]]
        assert parallel_cost["observed_attempt_count"] == 3, parallel_cost
        assert parallel_cost["tokens_in"] == 30 and parallel_cost["cost_usd"] == 3.0, parallel_cost

    print("strategy_experiment.py selftest: OK (plan, normalize, guarded prepare metadata)")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo")
    parser.add_argument("--spec-file")
    parser.add_argument("--exp-id")
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--hypothesis", help="Hypothesis id from experiments/hypotheses.json, e.g. H4"
    )
    source.add_argument("--arms-json", help="JSON list/object or path containing strategy arms")
    parser.add_argument("--hypotheses-path", help="Optional hypotheses JSON path")
    parser.add_argument(
        "--task-type",
        help=f"Task type, default {DEFAULT_TASK_TYPE} or hypothesis value",
    )
    parser.add_argument(
        "--prepare",
        action="store_true",
        help="Launch the underlying exp_abcd prepare phase",
    )
    parser.add_argument("--confirm-strategy", action="store_true", help="Required with --prepare")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args(list(argv))


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

        capabilities.production_heartbeat(
            "strategy-experiments", event_type, ref="strategy_experiment.main"
        )
    except Exception:
        pass


def main(argv: Sequence[str]) -> int:
    _capability_heartbeat()
    args = parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    missing = [name for name in ("repo", "spec_file", "exp_id") if not getattr(args, name)]
    if missing:
        print(
            f"missing required args: {', '.join('--' + m.replace('_', '-') for m in missing)}",
            file=sys.stderr,
        )
        return 2
    if not (args.hypothesis or args.arms_json):
        print("one of --hypothesis or --arms-json is required", file=sys.stderr)
        return 2
    try:
        plan = _build_plan_from_args(args)
        if args.prepare:
            if not args.confirm_strategy or os.environ.get("ORCH_STRATEGY_EXPERIMENT") != "1":
                print(
                    "--prepare requires --confirm-strategy and ORCH_STRATEGY_EXPERIMENT=1",
                    file=sys.stderr,
                )
                return 2
            plan = prepare_strategy_experiment(plan)
        if args.json:
            print(json.dumps(plan, indent=2, sort_keys=True))
        else:
            print(_format_plan(plan))
        return 0
    except Exception as exc:
        print(f"strategy_experiment.py: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
