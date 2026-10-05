"""Redirect chain 2/3: escalated lanes, sweep-sourced candidates, and count reporting."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import keepalive_shadow
import redirect_apply as ra


def test_an_escalated_pr_is_not_live_and_is_eligible():
    signals = {
        "pr_state": "open",
        "labels": ["needs-human", "agents:keepalive"],
        "consecutive_no_progress": 0,
        "rounds_without_task_completion": 0,
        "last_has_changes": False,
    }
    report = keepalive_shadow.synthesize_report(signals)
    assert report["state"] == "escalated"
    assert any(h.get("kind") == "escalation" for h in report.get("hints") or [])

    blocks = ra.lane_refusals(report, pid_checker=lambda _pid: None)
    assert not any("live lane" in b for b in blocks), blocks

    # Deliberate-break: escalated read as running again must fail the live-state contract.
    broken = dict(report, state="running")
    live_blocks = ra.lane_refusals(broken, pid_checker=lambda _pid: None)
    assert any("live lane" in b for b in live_blocks)


def test_a_sweep_stall_proposal_reaches_the_apply_candidate_list(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ra.redirect_shadow, "CORPUS_PATH", tmp_path / "corpus.jsonl")
    sweep_path = tmp_path / "redirect-sweep.json"
    stalled = {
        "target": "stranske/Orchestrator#999",
        "agent": "codex",
        "lane": "opener",
        "task_type": "implement",
        "state": "stalled",
        "recommended_action": "inspect",
        "policy_decision": {"action": "inspect", "confidence": "medium", "reason": "stalled"},
        "signals": {"has_worktree_changes": False},
        "drift": {"severity": "none", "findings": []},
        "hints": [],
        "errors": [],
    }
    sweep_path.write_text(
        json.dumps(
            {
                "generated_at": int(time.time()),
                "actionable": [stalled],
                "actionable_count": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps({"generated_at": int(time.time()), "plans": []}) + "\n", encoding="utf-8"
    )
    report_dir = tmp_path / "reports"
    report_dir.mkdir()

    screen = ra._screen(
        report_dir=report_dir,
        plan_path=plan_path,
        corpus_path=tmp_path / "corpus.jsonl",
        sweep_path=sweep_path,
        pid_checker=lambda _pid: None,
    )
    targets = [row["target"] for row in screen["rows"]]
    assert "stranske/Orchestrator#999" in targets
    assert screen["candidate_counts"]["sweep"] >= 1

    # Deliberate-break: without the sweep source the stalled target must not appear.
    screen_no_sweep = ra._screen(
        report_dir=report_dir,
        plan_path=plan_path,
        corpus_path=tmp_path / "corpus.jsonl",
        sweep_path=tmp_path / "missing-sweep.json",
        pid_checker=lambda _pid: None,
    )
    assert "stranske/Orchestrator#999" not in [row["target"] for row in screen_no_sweep["rows"]]


def test_the_run_prints_supervisor_sweep_and_eligible_counts():
    line = ra.format_candidate_counts({"supervisor": 2, "sweep": 1, "eligible": 1})
    assert line == "candidates: supervisor 2, sweep 1, eligible 1"
