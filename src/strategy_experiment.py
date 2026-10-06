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
import math
import os
import re
import shlex
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import claims
import exp_abcd
import feedback
import fleet_shapes
import research_scheduler
import synthesis_promotion

ORCH = Path(__file__).resolve().parent
DEFAULT_TASK_TYPE = "implement"
SUPPORTED_STRATEGIES = {"single", "parallel", "pair"}
DEFAULT_SUBJECT_INSTANCES = 3


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

    if strategy == "pair" and len(agents) != 2:
        raise ValueError(f"pair strategy must name implementer and reviewer: {agents!r}")
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
    if strategy == "pair":
        # A pair is deliberately not a parallel arm.  The reviewer receives the
        # implementer's artifact and must leave a review artifact before the
        # candidate can be evaluated (enforced by exp_abcd.collect()).
        members[0]["role"] = "implement"
        members[1]["role"] = "review"
        members[1]["review_of_member_id"] = members[0]["member_id"]
    return {
        "arm_id": arm_id,
        "strategy": strategy,
        "agents": agents,
        "members": members,
        "synthesize": synthesize,
        "label": label,
        "profile_id": arm_profile_id,
        "cost_basis": (
            "sum_agent_runs" if strategy in {"parallel", "pair"} else "single_agent_run"
        ),
    }


def subject_arms(
    agent: str, reviewer: str, *, instances: int = DEFAULT_SUBJECT_INSTANCES
) -> list[dict[str, Any]]:
    """Three independent single and implement-then-review instances.

    Keeping instances as separate arms preserves attribution.  In particular a
    review outcome can never be averaged into an unrelated implementation run.
    """
    if instances != DEFAULT_SUBJECT_INSTANCES:
        raise ValueError(f"strategy subject requires exactly {DEFAULT_SUBJECT_INSTANCES} instances")
    if not agent or not reviewer:
        raise ValueError("subject strategy requires an implementation agent and reviewer")
    arms: list[dict[str, Any]] = []
    for ordinal in range(1, instances + 1):
        arms.append({"strategy": "single", "agents": [agent], "instance": ordinal})
    for ordinal in range(1, instances + 1):
        arms.append({"strategy": "pair", "agents": [agent, reviewer], "instance": ordinal})
    return arms


def _shape_rate(shape: dict[str, Any]) -> float | None:
    rates = [
        float(cell["broke_later_rate"])
        for cell in (shape.get("agents") or {}).values()
        if isinstance(cell, dict)
        and isinstance(cell.get("broke_later_rate"), (int, float))
        and int(cell.get("bad", 0) or 0) + int(cell.get("durable", 0) or 0) >= 3
    ]
    return max(rates) if rates else None


def _open_pr_links_issue(node: dict[str, Any], repo: str | None = None) -> bool:
    number = int(node["number"])
    for event in (node.get("timelineItems") or {}).get("nodes") or []:
        pr = (event or {}).get("source") or {}
        if pr.get("__typename") != "PullRequest" or pr.get("state") != "OPEN":
            continue
        if repo and (pr.get("repository") or {}).get("nameWithOwner", repo).lower() != repo.lower():
            continue
        text = f"{pr.get('body') or ''}\n{pr.get('headRefName') or ''}".lower()
        if re.search(
            rf"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)[^#]*#{number}\b", text
        ) or re.search(rf"issue[-_/]{number}\b", text):
            return True
    return False


def _gh_subjects_for_shape(
    shape: dict[str, Any], target: str | None = None
) -> list[dict[str, Any]]:
    """Read current open issue bodies and linkage; failed reads stay UNKNOWN."""
    import subprocess

    rows: list[dict[str, Any]] = []
    query = """query($owner:String!,$name:String!){repository(owner:$owner,name:$name){pullRequests(first:100,states:OPEN){pageInfo{hasNextPage} nodes{__typename state body headRefName repository{nameWithOwner}}} issues(first:100,states:OPEN,orderBy:{field:CREATED_AT,direction:ASC}){pageInfo{hasNextPage} nodes{number body title updatedAt labels(first:30){nodes{name}} closedByPullRequestsReferences(first:100){pageInfo{hasNextPage} nodes{number}} timelineItems(first:100,itemTypes:[CROSS_REFERENCED_EVENT]){pageInfo{hasNextPage} nodes{...on CrossReferencedEvent{source{__typename ...on PullRequest{state body headRefName repository{nameWithOwner}}}}}}}}}}"""
    for repo in shape.get("repos") or []:
        if target and not target.lower().startswith(repo.lower() + "#"):
            continue
        try:
            owner, name = str(repo).split("/", 1)
            raw = subprocess.run(
                [
                    "gh",
                    "api",
                    "graphql",
                    "-f",
                    "query=" + query,
                    "-f",
                    "owner=" + owner,
                    "-f",
                    "name=" + name,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout
            live_repo = json.loads(raw)["data"]["repository"]
            issues = live_repo["issues"]
            open_prs = live_repo["pullRequests"]
            if open_prs["pageInfo"]["hasNextPage"]:
                raise RuntimeError("open PR population pagination incomplete")
            if issues["pageInfo"]["hasNextPage"]:
                raise RuntimeError("open issue population pagination incomplete")
            nodes = issues["nodes"]
        except Exception as exc:
            raise RuntimeError(f"UNKNOWN: live issue read failed for {repo}: {exc}") from exc
        for node in nodes:
            linked = node.get("closedByPullRequestsReferences") or {}
            if linked.get("pageInfo", {}).get("hasNextPage"):
                raise RuntimeError(
                    f"issue linkage pagination incomplete for {repo}#{node['number']}"
                )
            timeline = node.get("timelineItems") or {}
            if timeline.get("pageInfo", {}).get("hasNextPage"):
                raise RuntimeError(
                    f"issue timeline pagination incomplete for {repo}#{node['number']}"
                )
            node = {
                **node,
                "timelineItems": {
                    "nodes": [
                        *timeline.get("nodes", []),
                        *[{"source": pr} for pr in open_prs["nodes"]],
                    ]
                },
            }
            if (
                not linked.get("nodes")
                and not _open_pr_links_issue(node, repo)
                and node.get("body")
            ):
                rows.append(
                    {
                        "target": f"{repo}#{node['number']}",
                        "repo": repo,
                        "body": node["body"],
                        "title": node.get("title", ""),
                        "updated_at": node.get("updatedAt"),
                        "labels": [
                            x.get("name", "") for x in node.get("labels", {}).get("nodes", [])
                        ],
                    }
                )
    return rows


def select_subject(
    shapes_path: Path,
    *,
    target: str | None = None,
    issue_fetcher: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Select a current open, unlinked subject from the worst recurring shape.

    The shapes file has historical outcome rates only.  Current issue bodies and
    linkage are always acquired separately; incomplete acquisition is UNKNOWN.
    """
    try:
        payload = json.loads(shapes_path.read_text())
        shapes = payload["shapes"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"UNKNOWN: fleet-shapes unavailable or malformed: {exc}") from exc
    ranked: list[tuple[dict[str, Any], float]] = []
    for shape in shapes:
        if isinstance(shape, dict) and shape.get("recurring"):
            rate = _shape_rate(shape)
            if rate is not None:
                ranked.append((shape, rate))
    if not ranked:
        raise RuntimeError("UNKNOWN: no recurring shape with sufficient broke-later evidence")
    ranked.sort(
        key=lambda item: (
            -float(item[1]),
            -int(item[0].get("prs") or 0),
            str(item[0].get("key") or ""),
        )
    )
    fetch = issue_fetcher or (lambda shape: _gh_subjects_for_shape(shape, target))
    for shape, rate in ranked:
        candidates = [row for row in fetch(shape) if _matches_shape(row, shape)]
        if target:
            candidates = [
                row for row in candidates if str(row.get("target", "")).lower() == target.lower()
            ]
        if candidates:
            choice = sorted(candidates, key=lambda row: str(row["target"]))[0]
            return {
                **choice,
                "shape": shape["key"],
                "broke_later_rate": rate,
                "shape_match_basis": "predicted from issue body; implementation paths not yet observed",
                "shapes_generated_at": payload.get("generated_at"),
            }
    raise RuntimeError(
        "UNKNOWN: live reads completed but no open unlinked subject matched recurring shapes"
    )


def _matches_shape(issue: dict[str, Any], shape: dict[str, Any]) -> bool:
    """Keep experiments on implementation work resembling the selected shape.

    Issue bodies do not know their eventual file paths, so this only applies the
    observable commit-type/label part of a fleet shape; it deliberately does not
    fabricate a path match.
    """
    title = str(issue.get("title") or "").strip().lower()
    expected = str(shape.get("commit_type") or "other").lower()
    conventional = re.match(rf"(?:\[[^]]+\]\s*)?{re.escape(expected)}(?:\([^)]*\))?:", title)
    labels = {str(label).lower() for label in issue.get("labels") or []}
    body = str(issue.get("body") or "").lower()
    inferred = (
        expected in {"test", "tests"} and any(cue in title for cue in ("test", "coverage"))
    ) or (
        expected == "fix"
        and ("bug" in labels or any(cue in title for cue in ("fix", "bug", "failure")))
    )
    if expected != "other" and not conventional and not inferred:
        return False
    if {"needs-human", "agents:paused", "tracker:durable"} & labels or title.startswith(
        ("dependency dashboard", "agent metrics weekly summary")
    ):
        return False
    # The historical path shape cannot be known before implementation.  Require
    # an implementation-ready body signal instead of pretending a post-merge
    # label such as verify:compare predicts it.
    semantic = {
        "tests": ("test", "coverage", "pytest"),
        "code": ("implement", "function", "module", "api"),
        "docs": ("documentation", "readme"),
        "config": ("config", "yaml", "toml"),
    }
    cues = [cue for path in shape.get("path_classes") or [] for cue in semantic.get(path, ())]
    return (
        ("acceptance criteria" in body or ("must" in body and "test" in body))
        and bool(cues)
        and any(cue in body + "\n" + title for cue in cues)
    )


def freeze_subject_spec(subject: dict[str, Any], directory: Path) -> Path:
    """Persist the exact live issue body before any experiment work begins."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "spec.md"
    path.write_text(str(subject["body"]), encoding="utf-8")
    return path


def normalize_arms(arms: Sequence[Any]) -> list[dict[str, Any]]:
    normalized = [normalize_arm(arm, i) for i, arm in enumerate(arms)]
    if len(normalized) < 2:
        raise ValueError("strategy experiment needs at least two arms")
    return normalized


def implementation_agents(arms: Sequence[Any]) -> list[str]:
    normalized = (
        arms if arms and isinstance(arms[0], dict) and "arm_id" in arms[0] else normalize_arms(arms)
    )
    return _dedupe_preserve([agent for arm in normalized for agent in arm["agents"]])


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
                arm["members"][-1]["member_id"]
                if arm["strategy"] == "pair"
                else (
                    arm["members"][0]["member_id"]
                    if len(arm["members"]) == 1
                    else f"{exp_id}:artifact:{arm['arm_id']}:member-set"
                )
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
    if plan.get("subject"):
        metadata["subject"] = plan["subject"]
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


def prepare_subject_experiment(
    subject: dict[str, Any],
    *,
    exp_id: str,
    agent: str,
    reviewer: str,
    exp_dir: Path | None = None,
    claim_fn: Callable[[str, str], bool] = claims.claim,
    prepare_fn: Callable[
        [str, str, str, list[dict[str, Any]]], dict[str, Any]
    ] = exp_abcd.prepare_arms,
) -> dict[str, Any]:
    """Claim, freeze, then prepare the one explicitly confirmed subject experiment."""
    target = str(subject["target"])
    edir = (exp_dir or exp_abcd.EXP_DIR) / exp_id
    if (edir / "strategy.json").exists():
        raise RuntimeError(f"experiment already exists; use its followup path: {exp_id}")
    if not claim_fn(target, "research"):
        raise RuntimeError(f"subject claim refused: {target}")
    spec = freeze_subject_spec(subject, edir)
    plan = build_strategy_plan(
        str(subject["repo"]), str(spec), exp_id, subject_arms(agent, reviewer)
    )
    plan["metadata_path"] = str(edir / "strategy.json")
    plan["subject"] = {key: subject[key] for key in ("target", "shape", "broke_later_rate")}
    plan["subject"].update(
        {
            key: subject.get(key)
            for key in ("updated_at", "shape_match_basis", "shapes_generated_at")
        }
    )
    prepared = prepare_strategy_experiment(plan, prepare_fn=prepare_fn)
    meta_path = edir / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        meta["subject"] = plan["subject"]
        meta["frozen_issue_body"] = str(spec)
        meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    pids = [row["pid"] for row in prepared["prepared"].get("launched", []) if row.get("pid")]
    if pids:
        claims.update_metadata(
            target,
            "research",
            pid=0,
            pids=pids,
            refresh_ts=True,
            experiment_id=exp_id,
            lane="research",
            task_type="implement",
        )
    return prepared


def strategy_result_path(state_dir: Path | None = None) -> Path:
    root = state_dir or Path(
        os.environ.get("ORCH_STATE_DIR", Path.home() / ".codex" / "orchestrator")
    )
    return root / "capability-program" / "strategy-experiment.json"


def write_scored_result(
    plan: dict[str, Any],
    *,
    scores: dict[str, Any],
    cost_rows: Sequence[dict[str, Any]],
    state_dir: Path | None = None,
    result_path: Path | None = None,
) -> Path:
    """Write a comparison only when every arm has scored and cost coverage.

    Partial rows are explicitly UNKNOWN rather than a zero-cost, completed score.
    """
    costs = strategy_arm_costs(plan, cost_rows)
    missing = [arm_id for arm_id, row in costs.items() if row["missing_attempt_run_ids"]]
    expected_arms = {arm["arm_id"] for arm in plan["arms"]}
    expected_runs = {run_id for arm in plan["arms"] for run_id in arm.get("attempt_run_ids") or []}
    measured_cost_runs = {
        str(row.get("run_id"))
        for row in cost_rows
        if row.get("run_id")
        and isinstance(row.get("cost_usd"), (int, float))
        and math.isfinite(row["cost_usd"])
        and row["cost_usd"] >= 0
    }
    score_complete = (
        bool(expected_arms)
        and isinstance(scores, dict)
        and expected_arms <= set(scores)
        and all(
            isinstance(scores[key], (int, float))
            and not isinstance(scores[key], bool)
            and math.isfinite(scores[key])
            and 0 <= scores[key] <= 10
            for key in expected_arms
        )
    )
    status = (
        "completed"
        if score_complete and not missing and expected_runs <= measured_cost_runs
        else "UNKNOWN"
    )
    for row in costs.values():
        if not set(row["attempt_run_ids"]) <= measured_cost_runs:
            row["cost_usd"] = None
    comparison = {}
    if status == "completed":
        for strategy in sorted({str(arm.get("strategy", "unknown")) for arm in plan["arms"]}):
            ids = [arm["arm_id"] for arm in plan["arms"] if arm.get("strategy") == strategy]
            comparison[strategy] = {
                "instances": len(ids),
                "score_mean": sum(float(scores[key]) for key in ids) / len(ids),
                "cost_usd": round(sum(costs[key]["cost_usd"] for key in ids), 8),
                "arm_ids": ids,
            }
    payload = {
        "schema_version": 1,
        "status": status,
        "exp_id": plan["exp_id"],
        "subject": plan.get("subject"),
        "scores": scores if status == "completed" else None,
        "comparison": comparison if status == "completed" else None,
        "costs": costs,
        "unknown_reason": (
            None
            if status == "completed"
            else "incomplete scored evaluation or explicitly measured cost coverage"
        ),
        "written_ts": int(time.time()),
    }
    path = result_path or strategy_result_path(state_dir)
    fleet_shapes.write_json_atomic(path, payload)
    return path


def write_evaluation_result(
    exp_id: str, evaluators: Sequence[str], *, exp_dir: Path | None = None
) -> Path | None:
    """Project exact v2 evaluation rows and measured costs into the strategy state.

    A missing judge cell or cost row writes UNKNOWN; it never manufactures a
    completed comparison from an evaluator process merely having exited.
    """
    strategy_path = strategy_metadata_path(exp_id, exp_dir)
    if not strategy_path.exists():
        return None
    metadata = json.loads(strategy_path.read_text())
    arms = metadata.get("strategy_arms") or []
    plan = {"exp_id": exp_id, "arms": arms, "subject": metadata.get("subject")}
    final_to_arm = {str(arm.get("final_artifact_id")): str(arm.get("arm_id")) for arm in arms}
    scores: dict[str, list[float]] = {str(arm["arm_id"]): [] for arm in arms}
    cost_rows: list[dict[str, Any]] = []
    with feedback._conn() as conn:
        for member_id, arm_id in final_to_arm.items():
            rows = conn.execute(
                "SELECT evaluator_id,score FROM evaluations_v2 WHERE experiment_id=? AND implementer_member_id=?",
                (exp_id, member_id),
            ).fetchall()
            by_evaluator = {str(row[0]): row[1] for row in rows}
            if any(
                evaluator not in by_evaluator or by_evaluator[evaluator] is None
                for evaluator in evaluators
            ):
                scores.pop(arm_id, None)
                continue
            scores[arm_id] = [float(by_evaluator[evaluator]) for evaluator in evaluators]
        run_ids = [run_id for arm in arms for run_id in arm.get("attempt_run_ids") or []]
        for run_id in run_ids:
            row = conn.execute(
                "SELECT tokens_in,tokens_out,cost_usd,latency_s FROM costs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is not None:
                cost_rows.append(
                    {
                        "run_id": run_id,
                        "tokens_in": row[0],
                        "tokens_out": row[1],
                        "cost_usd": row[2],
                        "latency_s": row[3],
                    }
                )
    means = {arm_id: sum(values) / len(values) for arm_id, values in scores.items() if values}
    edir = strategy_path.parent
    # Preserve this trial before updating the shared switch-review projection.
    # Reading that shared file back could pick up a concurrent trial's result.
    receipt_path = write_scored_result(
        plan, scores=means, cost_rows=cost_rows, result_path=edir / "strategy-receipt.json"
    )
    receipt = json.loads(receipt_path.read_text())
    result_path = strategy_result_path()
    fleet_shapes.write_json_atomic(result_path, receipt)
    promotion_error = None
    try:
        promotion = synthesis_promotion.load_state(edir)
    except (OSError, ValueError, TypeError) as exc:
        promotion = None
        promotion_error = str(exc)[:300]
    receipt["promotion"] = None
    promoted = False
    if promotion is not None:
        candidate = promotion.get("candidate") or {}
        verification = promotion.get("verification") or {}
        candidate_path = edir / synthesis_promotion.CANDIDATE_JSON
        try:
            candidate_artifact = (
                json.loads(candidate_path.read_text()) if candidate_path.exists() else None
            )
        except (OSError, ValueError) as exc:
            candidate_artifact = None
            promotion_error = str(exc)[:300]
        promoted = (
            promotion.get("experiment_id") == exp_id
            and promotion["delivery_phase"]
            in {"candidate_ready", "delegated_or_pr", "merged", "durable"}
            and verification.get("passed") is True
            and bool(verification.get("evidence_hash"))
            and bool(candidate.get("candidate_id"))
            and candidate_artifact == candidate
            and candidate.get("verification_evidence_hash") == verification["evidence_hash"]
            and bool((promotion.get("synthesis") or {}).get("commit"))
            and (candidate.get("synthesis") or {}).get("commit") == promotion["synthesis"]["commit"]
            and (candidate.get("delivery") or {}).get("direct_publication_allowed") is False
        )
        receipt["promotion"] = {
            "delivery_phase": promotion["delivery_phase"],
            "candidate_id": candidate.get("candidate_id"),
            "synthesis_commit": (promotion.get("synthesis") or {}).get("commit"),
            "verification_evidence_hash": verification.get("evidence_hash"),
            "candidate_path": str(candidate_path),
            "verified_candidate": promoted,
        }
    receipt["acceptance_status"] = (
        "completed" if receipt["status"] == "completed" and promoted else "UNKNOWN"
    )
    receipt["promotion_error"] = promotion_error
    fleet_shapes.write_json_atomic(receipt_path, receipt)
    return result_path


def refresh_evaluation_result(exp_id: str, *, exp_dir: Path | None = None) -> Path | None:
    """Refresh late cost and promotion evidence without dispatching another judge."""
    if not strategy_metadata_path(exp_id, exp_dir).exists():
        return None
    edir = (exp_dir or exp_abcd.EXP_DIR) / exp_id
    maps = json.loads((edir / "eval-maps.json").read_text())
    if not isinstance(maps, dict) or not maps:
        raise ValueError("strategy receipt requires the original evaluator identities")
    return write_evaluation_result(exp_id, list(maps), exp_dir=exp_dir)


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
    parser.add_argument(
        "--refresh-result",
        action="store_true",
        help="Refresh the saved comparison/promotion receipt without launching agents",
    )
    parser.add_argument("--subject", help="Select a current open, unlinked issue from fleet shapes")
    parser.add_argument(
        "--shapes-path", type=Path, help="Override fleet-shapes.json for subject selection"
    )
    parser.add_argument("--agent", help="Implementation agent for --subject")
    parser.add_argument("--reviewer", help="Reviewer agent for --subject")
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
    if args.refresh_result:
        if not args.exp_id or args.prepare or args.subject:
            print(
                "--refresh-result requires --exp-id and cannot prepare a subject", file=sys.stderr
            )
            return 2
        try:
            if refresh_evaluation_result(args.exp_id) is None:
                raise ValueError("strategy experiment metadata not found")
            receipt_path = exp_abcd.EXP_DIR / args.exp_id / "strategy-receipt.json"
            receipt = json.loads(receipt_path.read_text())
            print(
                json.dumps(receipt, indent=2, sort_keys=True)
                if args.json
                else f"{receipt_path}: {receipt['acceptance_status']}"
            )
            return 0 if receipt["acceptance_status"] == "completed" else 1
        except (OSError, ValueError, TypeError) as exc:
            print(f"strategy_experiment.py: {exc}", file=sys.stderr)
            return 1
    if args.subject:
        if not args.exp_id or not args.agent or not args.reviewer:
            print("--subject requires --exp-id, --agent, and --reviewer", file=sys.stderr)
            return 2
        if (
            not args.prepare
            or not args.confirm_strategy
            or os.environ.get("ORCH_STRATEGY_EXPERIMENT") != "1"
        ):
            print(
                "--subject requires --prepare --confirm-strategy and ORCH_STRATEGY_EXPERIMENT=1",
                file=sys.stderr,
            )
            return 2
        try:
            shapes_path = args.shapes_path or (
                Path(os.environ.get("ORCH_STATE_DIR", Path.home() / ".codex" / "orchestrator"))
                / "fleet-shapes.json"
            )
            subject = select_subject(shapes_path, target=args.subject)
            plan = prepare_subject_experiment(
                subject, exp_id=args.exp_id, agent=args.agent, reviewer=args.reviewer
            )
            print(json.dumps(plan, indent=2, sort_keys=True) if args.json else _format_plan(plan))
            return 0
        except Exception as exc:
            print(f"strategy_experiment.py: {exc}", file=sys.stderr)
            return 1
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
