#!/usr/bin/env python3
"""keepalive_supervisor.py - staged, single-authority keepalive supervisor planner.

This is the first actionable step after the shadow corpus trigger arms. It does
not kill processes, release claims, relabel PRs, delegate, or apply redirect
plans. It only identifies keepalive PRs that have already escalated to human
attention and builds the report artifact a supervised RedirectAgent proposal can
use next.

Single-authority rule: eligible targets must be open keepalive PRs that already
carry a human-escalation label (`needs-human` or `agent:needs-attention`). While
keepalive is still actively controlling the PR, this module refuses live action.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import keepalive_shadow
import redirect_shadow

ESCALATION_LABELS = {"needs-human", "agent:needs-attention"}
ESCALATION_LABEL_ORDER = ["needs-human", "agent:needs-attention"]
KEEPALIVE_LABEL = "agents:keepalive"
PROPOSAL_ACTIONS = {"inspect", "redirect", "decompose"}
DEFAULT_AC = (
    "Post-escalation keepalive recovery only: inspect the PR after keepalive has escalated, "
    "preserve the original PR acceptance criteria, and do not act if the escalation label is gone."
)
DEFAULT_STAGE2_REPORT_DIR = Path.home() / ".codex" / "orchestrator" / "keepalive-supervisor-stage2"
DEFAULT_HISTORICAL_LIMIT = 10


def _label_set(signals: dict) -> set[str]:
    return {
        str(label).strip().lower() for label in signals.get("labels") or [] if str(label).strip()
    }


def _repo_num(target: str) -> tuple[str, str]:
    repo, sep, number = str(target or "").partition("#")
    if not sep or not repo or not number:
        raise ValueError("target must be owner/repo#N")
    return repo, number


def eligibility(signals: dict) -> dict:
    labels = _label_set(signals)
    blockers: list[str] = []
    if str(signals.get("pr_state") or "").lower() != "open":
        blockers.append("pr_not_open")
    if KEEPALIVE_LABEL not in labels:
        blockers.append("missing_agents_keepalive_label")
    if not labels.intersection(ESCALATION_LABELS):
        blockers.append("missing_human_escalation_label")
    return {
        "eligible": not blockers,
        "blockers": blockers,
        "single_authority": not blockers,
        "escalation_labels": sorted(labels.intersection(ESCALATION_LABELS)),
    }


def build_redirect_report(signals: dict, decision: dict) -> dict:
    report = keepalive_shadow.synthesize_report(signals)
    report.update(
        {
            "target": signals.get("target"),
            "agent": "keepalive",
            "lane": "closer",
            "task_type": "implement",
            "policy_decision": {
                "action": decision.get("shadow_action") or "inspect",
                "reason": decision.get("shadow_reason")
                or "post-escalation keepalive supervisor review",
                "confidence": "medium",
                "advisory": True,
            },
            "keepalive_supervisor": {
                "stage": "supervised_candidate",
                "live_action_enabled": False,
                "signals_summary": decision.get("signals_summary") or {},
            },
        }
    )
    return report


def plan_for_signals(
    signals: dict,
    *,
    report_json_path: Path | None = None,
    acceptance_criteria: str = DEFAULT_AC,
    proposal_backend: str | None = None,
) -> dict:
    decision = keepalive_shadow.shadow_decide(signals)
    gate = eligibility(signals)
    report = build_redirect_report(signals, decision)
    report_written = None
    if report_json_path:
        report_json_path.parent.mkdir(parents=True, exist_ok=True)
        report_json_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        report_written = str(report_json_path)

    action = decision.get("shadow_action") or "inspect"
    if not gate["eligible"]:
        next_step = "do_not_intervene"
        reason = "target is not in the post-escalation single-authority state"
    elif action in PROPOSAL_ACTIONS:
        next_step = "review_redirectagent_proposal"
        reason = "eligible for an operator-reviewed proposal; live apply remains disabled"
    else:
        next_step = "operator_inspect"
        reason = (
            "post-escalation target is eligible, but the shadow policy does not recommend recovery"
        )

    proposal_command = None
    stage2_record_command = None
    outcome_link_command_template = None
    if report_written and gate["eligible"]:
        proposal_command = [
            "python3",
            str(Path(__file__).resolve().parent / "roles.py"),
            "redirect",
            "--report-json",
            report_written,
            "--ac",
            acceptance_criteria,
        ]
        if proposal_backend:
            proposal_command.extend(["--backend", proposal_backend])
        proposal_command.append("--dispatch")
        stage2_record_command = proposal_command + ["--record-corpus", "--json"]
        outcome_link_command_template = [
            "python3",
            str(Path(__file__).resolve().parent / "redirect_shadow.py"),
            "link-outcome",
            "--role-run-id",
            "<role_run_id>",
            "--influenced-run-id",
            "<downstream_run_id>",
            "--entry-id",
            "<entry_id>",
            "--notes",
            "<operator outcome summary>",
            "--json",
        ]

    return {
        "target": signals.get("target"),
        "generated_at": int(time.time()),
        "stage": "supervised_candidate",
        "live_action_enabled": False,
        "eligible": gate["eligible"],
        "eligibility": gate,
        "shadow_decision": decision,
        "next_step": next_step,
        "reason": reason,
        "report": report,
        "report_written": report_written,
        "proposal_command": proposal_command,
        "stage2_record_command": stage2_record_command,
        "outcome_link_command_template": outcome_link_command_template,
        "acceptance_criteria": acceptance_criteria,
        "proposal_backend": proposal_backend or "",
    }


def _gh_search_targets(owner: str, label: str, limit: int, *, runner=subprocess.run) -> list[str]:
    result = runner(
        [
            "gh",
            "search",
            "prs",
            "--owner",
            owner,
            "--label",
            KEEPALIVE_LABEL,
            "--label",
            label,
            "--state",
            "open",
            "--limit",
            str(limit),
            "--json",
            "repository,number",
            "--jq",
            '.[] | "\\(.repository.nameWithOwner)#\\(.number)"',
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "gh search failed").strip()[-500:])
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]


def live_targets(owner: str = "stranske", limit: int = 20, *, runner=subprocess.run) -> list[str]:
    seen: set[str] = set()
    targets: list[str] = []
    per_label_limit = max(limit, 1)
    for label in ESCALATION_LABEL_ORDER:
        for target in _gh_search_targets(owner, label, per_label_limit, runner=runner):
            if target in seen:
                continue
            seen.add(target)
            targets.append(target)
            if len(targets) >= limit:
                return targets
    return targets


def plan_target(
    target: str,
    *,
    report_dir: Path | None = None,
    acceptance_criteria: str = DEFAULT_AC,
    proposal_backend: str | None = None,
) -> dict:
    signals = keepalive_shadow.gather_signals(target)
    report_path = None
    if report_dir:
        repo, number = _repo_num(target)
        safe = repo.replace("/", "__")
        report_path = report_dir / f"{safe}__{number}.keepalive-supervisor-report.json"
    return plan_for_signals(
        signals,
        report_json_path=report_path,
        acceptance_criteria=acceptance_criteria,
        proposal_backend=proposal_backend,
    )


def _stage2_deficits(summary: dict) -> dict:
    readiness_target = int(summary.get("readiness_target") or 0)
    linked_target = int(summary.get("linked_outcome_target") or 0)
    disagreement_target = int(summary.get("disagreement_outcome_target") or 0)
    valid = int(summary.get("valid_proposals") or 0)
    synced = int(summary.get("synced_role_outcomes") or 0)
    disagreements = int(summary.get("linked_disagreements") or 0)
    historical_linked = int(summary.get("historical_linked_proposals") or 0)
    historical_disagreements = int(summary.get("historical_linked_disagreements") or 0)
    return {
        "supervised_apply": {
            "valid_proposals": max(readiness_target - valid, 0),
            "synced_role_outcomes": max(linked_target - synced, 0),
            "linked_disagreements": max(disagreement_target - disagreements, 0),
        },
        "historical_replay_analysis": {
            "valid_proposals": max(readiness_target - valid, 0),
            "linked_or_historical_outcomes": max(linked_target - (synced + historical_linked), 0),
            "linked_or_historical_disagreements": max(
                disagreement_target - (disagreements + historical_disagreements), 0
            ),
        },
    }


def _historical_collect_command(
    *,
    historical_limit: int,
    backend: str | None = None,
    include_calibration: bool = False,
    corpus_path: Path | None = None,
    keepalive_corpus_path: Path | None = None,
) -> list[str]:
    cmd = [
        "python3",
        str(Path(__file__).resolve().parent / "redirect_shadow.py"),
        "collect-historical",
        "--limit",
        str(historical_limit),
    ]
    if backend:
        cmd.extend(["--backend", backend])
    if include_calibration:
        cmd.append("--include-calibration")
    if keepalive_corpus_path:
        cmd.extend(["--keepalive-corpus", str(keepalive_corpus_path)])
    if corpus_path:
        cmd.extend(["--corpus", str(corpus_path)])
    cmd.extend(["--dispatch", "--json"])
    return cmd


def _recorded_proposal_targets(
    corpus_path: Path, *, source: str | None = None, valid_only: bool = False
) -> set[str]:
    targets: set[str] = set()
    for row in redirect_shadow._iter_events(corpus_path):
        if row.get("kind") != "redirect_proposal":
            continue
        if source and row.get("source") != source:
            continue
        if valid_only and row.get("valid_proposal") is not True:
            continue
        target = row.get("target")
        if target:
            targets.add(str(target))
    return targets


def stage2_acquisition_plan(
    *,
    owner: str = "stranske",
    live_limit: int = 20,
    report_dir: Path | None = None,
    acceptance_criteria: str = DEFAULT_AC,
    historical_limit: int = DEFAULT_HISTORICAL_LIMIT,
    historical_backend: str | None = None,
    proposal_backend: str | None = None,
    include_calibration: bool = False,
    redirect_corpus_path: Path | None = None,
    keepalive_corpus_path: Path | None = None,
    runner=subprocess.run,
) -> dict:
    """Build the next operator step for Stage 2 evidence acquisition.

    This is intentionally read-only with respect to GitHub/claims/redirect plans.
    It may write local report JSON files so emitted live Stage 2 commands are
    runnable, and it emits an opt-in historical replay command when no live
    post-escalation candidates exist.
    """
    if live_limit <= 0:
        raise ValueError("live_limit must be positive")
    if historical_limit <= 0:
        raise ValueError("historical_limit must be positive")

    effective_report_dir = report_dir or DEFAULT_STAGE2_REPORT_DIR
    corpus_path = redirect_corpus_path or redirect_shadow.CORPUS_PATH
    targets = live_targets(owner=owner, limit=live_limit, runner=runner)
    plans: list[dict] = []
    errors: list[dict] = []
    for target in targets:
        try:
            plans.append(
                plan_target(
                    target,
                    report_dir=effective_report_dir,
                    acceptance_criteria=acceptance_criteria,
                    proposal_backend=proposal_backend,
                )
            )
        except Exception as exc:  # keep one bad PR from hiding other candidates
            errors.append({"target": target, "error": str(exc)})

    all_eligible = [
        plan for plan in plans if plan.get("eligible") and plan.get("stage2_record_command")
    ]
    recorded_live_targets = _recorded_proposal_targets(
        corpus_path, source="live-dispatch", valid_only=True
    )
    eligible = [plan for plan in all_eligible if plan.get("target") not in recorded_live_targets]
    already_recorded_eligible = [
        plan.get("target") for plan in all_eligible if plan.get("target") in recorded_live_targets
    ]
    summary = redirect_shadow.summarize(corpus_path)
    deficits = _stage2_deficits(summary)
    historical_preview = redirect_shadow.collect_historical_from_keepalive(
        keepalive_corpus_path=keepalive_corpus_path,
        limit=historical_limit,
        include_calibration=include_calibration,
        corpus_path=corpus_path,
    )
    historical_command = _historical_collect_command(
        historical_limit=historical_limit,
        backend=historical_backend,
        include_calibration=include_calibration,
        corpus_path=redirect_corpus_path,
        keepalive_corpus_path=keepalive_corpus_path,
    )
    calibration_preview = None
    calibration_command = None
    if (
        not include_calibration
        and int(historical_preview.get("would_collect") or 0) == 0
        and deficits["historical_replay_analysis"]["linked_or_historical_disagreements"] > 0
        and not summary.get("ready_for_historical_replay_analysis")
    ):
        calibration_preview = redirect_shadow.collect_historical_from_keepalive(
            keepalive_corpus_path=keepalive_corpus_path,
            limit=historical_limit,
            include_calibration=True,
            corpus_path=corpus_path,
        )
        calibration_command = _historical_collect_command(
            historical_limit=historical_limit,
            backend=historical_backend,
            include_calibration=True,
            corpus_path=redirect_corpus_path,
            keepalive_corpus_path=keepalive_corpus_path,
        )

    commands: list[dict] = []
    if eligible:
        commands.extend(
            {
                "kind": "live_stage2_record",
                "target": plan.get("target"),
                "command": plan["stage2_record_command"],
                "outcome_link_command_template": plan.get("outcome_link_command_template"),
            }
            for plan in eligible
        )
        status = "record_live_stage2_proposals"
        recommendation = (
            "execute the live stage2_record_command entries, review/apply accepted advice manually, "
            "then link downstream outcomes with outcome_link_command_template"
        )
    elif int(historical_preview.get("would_collect") or 0) > 0 and not summary.get(
        "ready_for_historical_replay_analysis"
    ):
        commands.append(
            {
                "kind": "historical_collect",
                "command": historical_command,
                "dry_run_preview": True,
            }
        )
        status = "collect_historical_replay"
        live_context = (
            "no unrecorded live post-escalation PRs remain"
            if all_eligible
            else "no eligible live post-escalation PRs exist"
        )
        recommendation = (
            f"{live_context}; run the historical collect command to add bounded blinded RedirectAgent "
            "replay evidence without enabling live supervision"
        )
    elif calibration_preview and int(calibration_preview.get("would_collect") or 0) > 0:
        commands.append(
            {
                "kind": "historical_collect_calibration",
                "command": calibration_command,
                "dry_run_preview": True,
            }
        )
        status = "collect_calibration_replay"
        recommendation = (
            "strict historical replay candidates are exhausted but disagreement evidence is still thin; "
            "run the calibration collect command to add bounded success-case disagreement replays"
        )
    elif summary.get("ready_for_supervised_apply"):
        status = "ready_for_supervised_apply_review"
        recommendation = "Stage 2 synced evidence is ready; review Stage 3 design before implementing any apply path"
    elif summary.get("ready_for_historical_replay_analysis"):
        status = "historical_replay_ready_wait_for_live_links"
        recommendation = (
            "historical replay evidence is ready for counterfactual analysis; wait for live accepted advice "
            "and synced outcome links before supervised apply can be considered"
        )
    else:
        status = "waiting_for_candidates"
        recommendation = (
            "keep shadow/backfill collection running; no eligible live targets or unreplayed historical "
            "candidates are currently available"
        )

    return {
        "generated_at": int(time.time()),
        "stage": "stage2_proposal_corpus",
        "live_action_enabled": False,
        "owner": owner,
        "live_limit": live_limit,
        "report_dir": str(effective_report_dir),
        "live_candidate_count": len(targets),
        "eligible_live_candidate_count": len(all_eligible),
        "unrecorded_live_candidate_count": len(eligible),
        "already_recorded_live_targets": sorted(t for t in already_recorded_eligible if t),
        "live_targets": targets,
        "live_plan_errors": errors,
        "plans": plans,
        "redirect_corpus_path": str(corpus_path),
        "stage2_summary": summary,
        "deficits": deficits,
        "historical_limit": historical_limit,
        "proposal_backend": proposal_backend or "",
        "historical_preview": historical_preview,
        "historical_collect_command": historical_command,
        "calibration_preview": calibration_preview,
        "calibration_collect_command": calibration_command,
        "commands": commands,
        "status": status,
        "recommendation": recommendation,
    }


def _selftest() -> None:
    import tempfile

    eligible_signals = keepalive_shadow.normalize_signals(
        "o/r#7",
        {
            "failure": {"count": 3},
            "failure_threshold": 3,
            "consecutive_zero_activity_rounds": 0,
            "last_files_changed": 0,
        },
        pr_state="OPEN",
        labels=[KEEPALIVE_LABEL, "needs-human"],
    )
    with tempfile.TemporaryDirectory(prefix="keepalive-supervisor-") as tmp:
        report_path = Path(tmp) / "report.json"
        plan = plan_for_signals(eligible_signals, report_json_path=report_path)
        assert plan["eligible"] is True, plan
        assert plan["live_action_enabled"] is False, plan
        assert plan["next_step"] in {
            "review_redirectagent_proposal",
            "operator_inspect",
        }, plan
        assert report_path.exists(), plan
        assert plan["proposal_command"] and "--dispatch" in plan["proposal_command"], plan
        assert (
            plan["stage2_record_command"] and "--record-corpus" in plan["stage2_record_command"]
        ), plan
        assert plan["stage2_record_command"][-1] == "--json", plan
        assert (
            plan["outcome_link_command_template"]
            and "link-outcome" in plan["outcome_link_command_template"]
        ), plan
        report = json.loads(report_path.read_text())
        assert (
            report["target"] == "o/r#7"
            and report["keepalive_supervisor"]["stage"] == "supervised_candidate"
        ), report

    active_signals = keepalive_shadow.normalize_signals(
        "o/r#8",
        {"last_files_changed": 1},
        pr_state="OPEN",
        labels=[KEEPALIVE_LABEL],
    )
    blocked = plan_for_signals(active_signals)
    assert blocked["eligible"] is False, blocked
    assert "missing_human_escalation_label" in blocked["eligibility"]["blockers"], blocked
    assert blocked["proposal_command"] is None, blocked
    assert blocked["stage2_record_command"] is None, blocked

    closed_signals = keepalive_shadow.normalize_signals(
        "o/r#9",
        {"last_files_changed": 1},
        pr_state="MERGED",
        labels=[KEEPALIVE_LABEL, "needs-human"],
    )
    closed = plan_for_signals(closed_signals)
    assert (
        closed["eligible"] is False and "pr_not_open" in closed["eligibility"]["blockers"]
    ), closed

    calls: list[list[str]] = []

    def fake_runner(cmd, capture_output=True, text=True):
        calls.append(cmd)
        if "needs-human" in cmd:
            out = "stranske/Repo#1\nstranske/Repo#2\n"
        else:
            out = "stranske/Repo#2\nstranske/Repo#3\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    targets = live_targets(owner="stranske", limit=3, runner=fake_runner)
    assert targets == ["stranske/Repo#1", "stranske/Repo#2", "stranske/Repo#3"], targets
    assert len(calls) == 2, calls

    def empty_runner(cmd, capture_output=True, text=True):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with tempfile.TemporaryDirectory(prefix="keepalive-supervisor-stage2-") as tmp:
        tmp_path = Path(tmp)
        keepalive_corpus = tmp_path / "keepalive-shadow.jsonl"
        redirect_corpus = tmp_path / "redirect-shadow.jsonl"
        keepalive_shadow.record(
            {
                "target": "o/r#10",
                "source": "shadow",
                "outcome": "needs_human",
                "watch_state": "progress",
                "keepalive_blunt": "needs-human",
                "shadow_action": "redirect",
                "disagreement": True,
                "signals_summary": {"failure_count": 3},
            },
            keepalive_corpus,
        )
        stage2 = stage2_acquisition_plan(
            live_limit=2,
            historical_limit=5,
            report_dir=tmp_path / "reports",
            redirect_corpus_path=redirect_corpus,
            keepalive_corpus_path=keepalive_corpus,
            runner=empty_runner,
        )
        assert stage2["status"] == "collect_historical_replay", stage2
        assert stage2["live_candidate_count"] == 0, stage2
        assert stage2["historical_preview"]["would_collect"] == 1, stage2
        assert stage2["commands"][0]["kind"] == "historical_collect", stage2
        assert "--dispatch" in stage2["historical_collect_command"], stage2
        assert "--keepalive-corpus" in stage2["historical_collect_command"], stage2
        assert (
            stage2["deficits"]["supervised_apply"]["valid_proposals"]
            == redirect_shadow.READINESS_TARGET
        ), stage2

        def one_live_runner(cmd, capture_output=True, text=True):
            out = "stranske/Repo#77\n" if "needs-human" in cmd else ""
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

        original_plan_target = globals()["plan_target"]

        def fake_plan_target(
            target,
            *,
            report_dir=None,
            acceptance_criteria=DEFAULT_AC,
            proposal_backend=None,
        ):
            command = [
                "python3",
                str(Path(__file__).resolve().parent / "roles.py"),
                "redirect",
                "--report-json",
                str((report_dir or tmp_path) / "repo77.json"),
                "--ac",
                acceptance_criteria,
            ]
            if proposal_backend:
                command.extend(["--backend", proposal_backend])
            command.extend(["--dispatch", "--record-corpus", "--json"])
            return {
                "target": target,
                "eligible": True,
                "stage2_record_command": command,
                "outcome_link_command_template": [
                    "python3",
                    "redirect_shadow.py",
                    "link-outcome",
                ],
            }

        try:
            globals()["plan_target"] = fake_plan_target
            _append = redirect_shadow._append_event
            _append(
                {
                    "kind": "redirect_proposal",
                    "schema_version": redirect_shadow.SCHEMA_VERSION,
                    "entry_id": "invalid-live-dispatch",
                    "source": "live-dispatch",
                    "target": "stranske/Repo#77",
                    "valid_proposal": False,
                    "errors": ["backend parse failed"],
                },
                redirect_corpus,
            )
            retry_live = stage2_acquisition_plan(
                live_limit=2,
                historical_limit=5,
                report_dir=tmp_path / "reports",
                redirect_corpus_path=redirect_corpus,
                keepalive_corpus_path=keepalive_corpus,
                proposal_backend="cursor",
                runner=one_live_runner,
            )
            assert retry_live["status"] == "record_live_stage2_proposals", retry_live
            assert retry_live["unrecorded_live_candidate_count"] == 1, retry_live
            assert retry_live["already_recorded_live_targets"] == [], retry_live
            assert (
                "--backend" in retry_live["commands"][0]["command"]
                and "cursor" in retry_live["commands"][0]["command"]
            ), retry_live
            _append(
                {
                    "kind": "redirect_proposal",
                    "schema_version": redirect_shadow.SCHEMA_VERSION,
                    "entry_id": "valid-live-dispatch",
                    "source": "live-dispatch",
                    "target": "stranske/Repo#77",
                    "valid_proposal": True,
                    "errors": [],
                },
                redirect_corpus,
            )
            recorded_live = stage2_acquisition_plan(
                live_limit=2,
                historical_limit=5,
                report_dir=tmp_path / "reports",
                redirect_corpus_path=redirect_corpus,
                keepalive_corpus_path=keepalive_corpus,
                proposal_backend="cursor",
                runner=one_live_runner,
            )
            assert recorded_live["unrecorded_live_candidate_count"] == 0, recorded_live
            assert recorded_live["already_recorded_live_targets"] == [
                "stranske/Repo#77"
            ], recorded_live
        finally:
            globals()["plan_target"] = original_plan_target
    print("keepalive_supervisor.py selftest: OK")


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
            "live-keepalive-supervisor", event_type, ref="keepalive_supervisor.main"
        )
    except Exception:
        pass


def main(argv: list[str]) -> int:
    _capability_heartbeat()
    parser = argparse.ArgumentParser(
        description="Plan supervised post-escalation keepalive recovery; no live action."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--target", help="owner/repo#N to inspect and plan")
    group.add_argument(
        "--signals-json", type=Path, help="offline normalized keepalive signals JSON"
    )
    group.add_argument(
        "--list-live",
        action="store_true",
        help="list and plan open post-escalation keepalive PRs",
    )
    group.add_argument(
        "--stage2-plan",
        action="store_true",
        help="plan the next Stage 2 proposal-corpus acquisition step",
    )
    parser.add_argument("--owner", default="stranske", help="owner/org used by --list-live")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument(
        "--write-report-dir",
        type=Path,
        help="write RedirectAgent report JSON files here",
    )
    parser.add_argument("--acceptance-criteria", default=DEFAULT_AC)
    parser.add_argument("--historical-limit", type=int, default=DEFAULT_HISTORICAL_LIMIT)
    parser.add_argument(
        "--historical-backend",
        default="",
        help="backend to force in emitted historical collect command",
    )
    parser.add_argument(
        "--stage2-backend",
        default="",
        help="backend to force in emitted live Stage 2 RedirectAgent commands",
    )
    parser.add_argument(
        "--include-calibration",
        action="store_true",
        help="include lower-priority success-calibration replay cases",
    )
    parser.add_argument(
        "--redirect-corpus", type=Path, help="override RedirectAgent shadow corpus path"
    )
    parser.add_argument(
        "--keepalive-corpus", type=Path, help="override keepalive shadow corpus path"
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)

    if args.selftest:
        _selftest()
        return 0
    if args.limit <= 0:
        parser.error("--limit must be positive")
    if args.historical_limit <= 0:
        parser.error("--historical-limit must be positive")

    if args.signals_json:
        signals = json.loads(args.signals_json.read_text())
        result: dict[str, Any] = plan_for_signals(
            signals,
            report_json_path=(
                args.write_report_dir / "offline.keepalive-supervisor-report.json"
                if args.write_report_dir
                else None
            ),
            acceptance_criteria=args.acceptance_criteria,
            proposal_backend=args.stage2_backend or None,
        )
    elif args.target:
        result = plan_target(
            args.target,
            report_dir=args.write_report_dir,
            acceptance_criteria=args.acceptance_criteria,
            proposal_backend=args.stage2_backend or None,
        )
    elif args.list_live:
        targets = live_targets(owner=args.owner, limit=args.limit)
        result = {
            "generated_at": int(time.time()),
            "live_action_enabled": False,
            "targets": targets,
            "plans": [
                plan_target(
                    target,
                    report_dir=args.write_report_dir,
                    acceptance_criteria=args.acceptance_criteria,
                    proposal_backend=args.stage2_backend or None,
                )
                for target in targets
            ],
        }
    elif args.stage2_plan:
        result = stage2_acquisition_plan(
            owner=args.owner,
            live_limit=args.limit,
            report_dir=args.write_report_dir,
            acceptance_criteria=args.acceptance_criteria,
            historical_limit=args.historical_limit,
            historical_backend=args.historical_backend or None,
            proposal_backend=args.stage2_backend or None,
            include_calibration=args.include_calibration,
            redirect_corpus_path=args.redirect_corpus,
            keepalive_corpus_path=args.keepalive_corpus,
        )
    else:
        parser.error(
            "one of --target, --signals-json, --list-live, --stage2-plan, or --selftest is required"
        )

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        if "plans" in result:
            if result.get("stage") == "stage2_proposal_corpus":
                print(f"stage2_status={result['status']}")
                print(f"recommendation={result['recommendation']}")
                print(
                    "live_candidates="
                    f"{result['unrecorded_live_candidate_count']} unrecorded / "
                    f"{result['eligible_live_candidate_count']} eligible / "
                    f"{result['live_candidate_count']} total "
                    f"historical_would_collect={result['historical_preview'].get('would_collect', 0)}"
                )
                for item in result.get("commands") or []:
                    print(f"{item['kind']}_command=" + " ".join(item["command"]))
            else:
                print(f"post-escalation keepalive targets: {len(result['targets'])}")
            for plan in result["plans"]:
                print(f"- {plan['target']}: eligible={plan['eligible']} next={plan['next_step']}")
                if plan.get("stage2_record_command"):
                    print("  stage2_record_command=" + " ".join(plan["stage2_record_command"]))
        else:
            print(
                f"target={result.get('target')} eligible={result.get('eligible')} next={result.get('next_step')}"
            )
            print(f"reason={result.get('reason')}")
            if result.get("proposal_command"):
                print("proposal_command=" + " ".join(result["proposal_command"]))
            if result.get("stage2_record_command"):
                print("stage2_record_command=" + " ".join(result["stage2_record_command"]))
            if result.get("outcome_link_command_template"):
                print(
                    "outcome_link_command_template="
                    + " ".join(result["outcome_link_command_template"])
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
