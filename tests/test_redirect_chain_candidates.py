"""Redirect chain 2/3: escalated lanes, sweep-sourced candidates, and count reporting."""

from __future__ import annotations

import io
import json
import os
import time
from contextlib import ExitStack, contextmanager, redirect_stdout
from unittest.mock import patch

import keepalive_shadow
import redirect_apply as ra
import redirect_sweep


@contextmanager
def _stores(tmp_path):
    corpus = tmp_path / "corpus.jsonl"
    sweep = tmp_path / "redirect-sweep.json"
    reports = tmp_path / "reports"
    reports.mkdir()
    with ExitStack() as patches:
        patches.enter_context(patch.object(ra.redirect_shadow, "CORPUS_PATH", corpus))
        patches.enter_context(patch.object(redirect_sweep, "DEFAULT_REPORT", sweep))
        patches.enter_context(patch.object(ra.claims, "holder", lambda _target: None))
        patches.enter_context(
            patch.object(
                ra, "preview_link_applied_outcomes", lambda **_kwargs: {"pending": 0, "links": []}
            )
        )
        patches.enter_context(patch.dict(os.environ, {ra.BOOTSTRAP_FLAG: "1"}))
        yield {
            "corpus": corpus,
            "sweep": sweep,
            "reports": reports,
            "plan": tmp_path / "plan.json",
            "now": int(time.time()),
        }


def _report(target, state="stalled"):
    return {
        "target": target,
        "agent": "codex",
        "lane": "opener",
        "task_type": "implement",
        "state": state,
        "recommended_action": "inspect",
        "policy_decision": {"action": "inspect", "confidence": "medium", "reason": state},
        "signals": {"has_worktree_changes": False},
        "drift": {"severity": "none", "findings": []},
        "hints": [],
        "errors": [],
    }


def _write(path, data):
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")


def test_an_escalated_pr_is_not_live_and_is_eligible(tmp_path):
    with _stores(tmp_path):
        for labels, payload, details in [
            (["needs-human"], {}, ["needs-human"]),
            ([" Agent:Needs-Attention "], {}, ["agent:needs-attention"]),
            (
                ["needs-human", "agent:needs-attention"],
                {},
                ["needs-human", "agent:needs-attention"],
            ),
            (
                [],
                {"attention": {"disposition": "needs-human"}},
                ["keepalive-state attention.disposition=needs-human"],
            ),
            (
                [],
                {"attention": {"disposition": "challenge-due"}},
                ["keepalive-state attention.disposition=challenge-due"],
            ),
        ]:
            signals = keepalive_shadow.normalize_signals(
                "o/r#1",
                {**payload, "last_files_changed": 2, "rounds_without_task_completion": 2},
                pr_state="open",
                labels=[*labels, "agents:keepalive"],
            )
            assert signals["outcome"] == "needs_human"
            report = keepalive_shadow.synthesize_report(signals)
            assert report["state"] == "escalated"
            evidence = sorted(h["detail"] for h in report["hints"] if h["kind"] == "escalation")
            assert evidence == sorted(details)
            assert not ra.lane_refusals(report)
            screened = ra.screen_report(
                {**report, "target": "o/r#1", "agent": "keepalive"},
                gate={"bootstrap_needed": True},
                applied_targets=set(),
                applies_today=0,
            )
            assert screened["passes_screen"], screened
            # An escalation cannot override a process that is demonstrably still alive.
            assert ra.LIVE_PID_BLOCK in ra.lane_refusals(
                {**report, "pid": 123}, pid_checker=lambda _pid: True
            )
            for terminal in ("merged", "closed"):
                terminal_report = keepalive_shadow.synthesize_report(
                    {**signals, "pr_state": terminal}
                )
                assert terminal_report["state"] == "exited"

        # An ordinary automation retry or malformed marker does not prove escalation.
        for attention in (None, "needs-human", {}, {"disposition": "automation-retry"}):
            signals = keepalive_shadow.normalize_signals(
                "o/r#1", {"attention": attention}, pr_state="open", labels=[]
            )
            assert keepalive_shadow.synthesize_report(signals)["state"] == "running"

        # Deliberate-break: escalated read as running again must fail the live-state contract.
        broken = dict(report, state="running")
        live_blocks = ra.lane_refusals(broken, pid_checker=lambda _pid: None)
        assert any("live lane" in b for b in live_blocks)


def test_a_sweep_stall_proposal_reaches_the_apply_candidate_list(tmp_path):
    with _stores(tmp_path) as stores:
        stalled = _report("stranske/Orchestrator#999")
        supervisor = _report("o/r#1", "escalated")
        plan = {
            "generated_at": stores["now"],
            "plans": [{"eligible": True, "report": supervisor, "acceptance_criteria": "AC"}] * 2,
        }
        sweep = {
            "generated_at": stores["now"],
            "actionable": [stalled, stalled, _report("o/r#1"), _report("o/r#2", "running")],
        }
        _write(stores["plan"], plan)
        _write(stores["sweep"], sweep)
        kwargs = {
            "report_dir": stores["reports"],
            "plan_path": stores["plan"],
            "corpus_path": stores["corpus"],
            "sweep_path": stores["sweep"],
            "now": stores["now"],
        }
        screen = ra._screen(**kwargs)
        assert [row["target"] for row in screen["rows"]] == ["o/r#1", stalled["target"]]
        assert screen["rows"][0]["report"]["state"] == "escalated"
        assert screen["candidate_counts"] == {"supervisor": 1, "sweep": 1, "eligible": 2}
        candidates, _meta = ra.sweep_stalled_candidates(stores["sweep"], now=stores["now"])
        assert all(c["source"] == "redirect-sweep-live" for c in candidates)

        # The independent sweep source remains usable when the supervisor is stale or missing.
        for timestamp in (0, None):
            if timestamp is None:
                stores["plan"].unlink()
            else:
                _write(stores["plan"], {**plan, "generated_at": timestamp})
            screen = ra._screen(**kwargs)
            assert stalled["target"] in [r["target"] for r in screen["passing"]]
            assert screen["candidate_counts"] == {"supervisor": 0, "sweep": 2, "eligible": 2}
            out = ra.apply_candidates(**kwargs, max_offloads=0)
            assert out["candidate_counts"] == screen["candidate_counts"]

        # Deliberate-break: without the sweep source the stalled target must not appear.
        screen_no_sweep = ra._screen(
            **{**kwargs, "sweep_path": stores["sweep"].with_name("missing.json")}
        )
        assert "stranske/Orchestrator#999" not in [row["target"] for row in screen_no_sweep["rows"]]
        for data in ([stalled], {**sweep, "actionable": {}}, {**sweep, "generated_at": 0}):
            _write(stores["sweep"], data)
            assert not ra._screen(**kwargs)["rows"]


def test_the_run_prints_supervisor_sweep_and_eligible_counts(tmp_path):
    with _stores(tmp_path) as stores:
        line = ra.format_candidate_counts({"supervisor": 2, "sweep": 1, "eligible": 1})
        assert line == "candidates: supervisor 2, sweep 1, eligible 1"
        _write(stores["plan"], {"generated_at": stores["now"], "plans": []})
        _write(stores["sweep"], {"generated_at": stores["now"], "actionable": [_report("o/r#3")]})
        for mode in ("--screen", "--status", "--apply"):
            output = io.StringIO()
            with redirect_stdout(output):
                assert ra.main(
                    [
                        mode,
                        "--stage2-plan",
                        str(stores["plan"]),
                        "--corpus",
                        str(stores["corpus"]),
                        "--report-dir",
                        str(stores["reports"]),
                        "--max-offloads",
                        "0",
                    ]
                ) == 0
            assert output.getvalue().splitlines().count(
                "candidates: supervisor 0, sweep 1, eligible 1"
            ) == 1
