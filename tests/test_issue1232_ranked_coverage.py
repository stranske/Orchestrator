"""Hermetic coverage tests for the #1232 ranked reporting targets."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

import feedback
import keepalive_supervisor as supervisor
import relearn_report
import runtime_ac
import runtime_ac_panel as panel


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")


def _spec_and_gate():
    spec = runtime_ac._valid_spec()
    gate = runtime_ac.evaluate_results(spec, {"check_results": panel._all_check_results(spec)})
    return spec, gate


def test_panel_prompt_normalization_parsing_and_dispatch(monkeypatch):
    spec, gate = _spec_and_gate()
    monkeypatch.setattr(feedback, "active_evidence_types", lambda: [])
    assert "none currently registered" in panel.build_review_prompt(spec, gate)
    monkeypatch.setattr(
        feedback, "active_evidence_types", lambda: [{"name": "trace", "rationale": "fresh"}]
    )
    assert "trace - fresh" in panel.build_review_prompt(spec, gate)
    with pytest.raises(ValueError, match="invalid runtime AC spec"):
        panel.build_review_prompt({}, gate)

    review = panel._normalize_review(
        {
            "reviewer": " ",
            "verdict": "unknown",
            "ac_assessments": [None, {"ac_id": "AC1", "status": "pass", "reason": "ok"}],
            "blockers": [None, {"finding": "", "severity": "high"}],
            "evidence_gaps": [" ", "need trace"],
        },
        1,
        {"AC1"},
    )
    assert review["reviewer"] == ""
    assert review["verdict"] == "NEEDS_REVIEW"
    assert review["ac_assessments"][0]["status"] == "PASS"
    assert review["evidence_gaps"] == ["need trace"]
    with pytest.raises(ValueError, match="must be an object"):
        panel._normalize_review("bad", 0, set())
    assert panel.parse_review_output('prefix {"verdict":"PASS"} suffix', "v")["reviewer"] == "v"
    assert (
        panel.parse_review_output('```json\n{"reviews":[{"verdict":"PASS"}]}\n```', "v")["verdict"]
        == "PASS"
    )
    with pytest.raises(ValueError, match="no parseable JSON"):
        panel.parse_review_output("not json", "v")
    with pytest.raises(ValueError, match="no review objects"):
        panel.parse_review_output('{"reviews": []}', "v")
    assert panel._parse_reviewers(" a, ,b ") == ["a", "b"]
    with pytest.raises(ValueError, match="at least one"):
        panel._parse_reviewers(" , ")

    def offload(agent, _prompt, **_kwargs):
        if agent == "bad":
            return {"agent": agent, "exit": 0, "output": "not json"}
        if agent == "boom":
            raise RuntimeError("offline")
        return {"agent": agent, "exit": 0, "output": '{"verdict":"PASS"}'}

    result = panel.dispatch_reviewers(spec, gate, ["good", "bad", "boom"], offload_fn=offload)
    assert [r["verdict"] for r in result["review_doc"]["reviews"]] == [
        "PASS",
        "NEEDS_REVIEW",
        "NEEDS_REVIEW",
    ]
    assert result["dispatches"][0]["parsed"] is True
    assert result["dispatches"][1]["error"].startswith("no parseable JSON")


def test_relearn_report_error_dry_run_formatting_and_cli(monkeypatch, capsys):
    monkeypatch.setattr(relearn_report, "_role_priors_from_registry", lambda _priors: {})
    assert relearn_report.priors_from_route_table(
        {"x": {"neutral_prior": True, "agents": [{"agent": "a"}]}}
    ) == {"x": {"a": 0.5}}
    assert (
        relearn_report.blend_bt_priors({"x": {"a": 0.7}}, bt_fn=lambda **_k: {"ready": False}) == {}
    )
    priors = {"x": {"a": 0.7, "b": 0.65}}
    assert relearn_report.blend_bt_priors(
        priors, bt_fn=lambda **_k: {"ready": True, "strengths": {"b": 1.0}, "comparisons": 4}
    ) == {"x": 4}
    assert priors["x"]["b"] == 0.675
    assert (
        relearn_report._ordered_rows(
            [{"agent": "b", "score": None}, {"agent": "a", "score": 1.0}], ["a", "b"]
        )[0]["agent"]
        == "a"
    )
    monkeypatch.setattr(feedback, "_conn", lambda: (_ for _ in ()).throw(OSError("offline")))
    assert "error" in relearn_report.cost_telemetry_summary({"x": {"a": 0.7}}, 1)
    assert (
        relearn_report._fmt_cost_telemetry({"error": "offline"})
        == "cost telemetry: UNREADABLE (offline)"
    )
    assert relearn_report._fmt_num(None) == "n/a" and relearn_report._fmt_delta(2) == "+2"
    with pytest.raises(Exception, match="positive"):
        relearn_report._positive_int("0")

    report = {
        "dry_run": True,
        "learner": "fake",
        "window_days": 3,
        "would_write_version": 2,
        "learner_note": "note",
        "cost_telemetry": {"a": {"measured": False, "coverage": None}},
        "tasks": [
            {
                "task_type": "x",
                "prior_order": ["a"],
                "rows": [{"prior_rank": 1, "agent": "a", "prior": 0.7}],
            }
        ],
    }
    assert "No route_weights version was written" in relearn_report.format_human(report)
    learned = {
        "dry_run": False,
        "learner": "fake",
        "window_days": 3,
        "new_version": 2,
        "previous_version": 1,
        "learner_note": "note",
        "cost_telemetry": {"a": {"measured": True, "coverage": 1.0}},
        "tasks": [
            {
                "task_type": "x",
                "diverges_from_prior": True,
                "prior_order": ["a"],
                "learned_order": ["a"],
                "previous_order": [],
                "note": "cold",
                "rows": [
                    {
                        "agent": "a",
                        "posterior": 0.7,
                        "score": 0.8,
                        "n_obs": 2,
                        "learned_rank": 1,
                        "prior_rank_delta": 0,
                        "previous_rank_delta": None,
                    }
                ],
            }
        ],
    }
    assert "DIVERGED" in relearn_report.format_human(learned)
    monkeypatch.setattr(relearn_report, "build_report", lambda **_kwargs: report)
    assert relearn_report.main(["--dry-run", "--json", "--window-days", "3"]) == 0
    assert '"dry_run": true' in capsys.readouterr().out


def test_keepalive_supervisor_hermetic_closure_and_offline_paths(monkeypatch, tmp_path, capsys):
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(supervisor.subprocess, "run", run)
    assert supervisor._closure_gh(["api", "x"], pages=True) == {"ok": True}
    assert commands[0][-2:] == ["--paginate", "--slurp"]
    monkeypatch.setattr(
        supervisor.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=1, stderr="no", stdout=""),
    )
    with pytest.raises(RuntimeError, match="no"):
        supervisor._closure_gh(["api", "x"])
    monkeypatch.setattr(
        supervisor, "_closure_gh", lambda *_a, **_k: {"data": {"repository": {"pullRequest": None}}}
    )
    with pytest.raises(ValueError, match="incomplete"):
        supervisor._closure_pr("o/r#1")
    assert (
        supervisor._closure_gate("o/r#1", {"body": "no headings", "target": "o/r#1"}, "a" * 40)[
            "reason"
        ]
        == "no gate commands"
    )
    monkeypatch.setitem(
        sys.modules, "backlog", SimpleNamespace(scoped_blocker_source=lambda: "bad")
    )
    with pytest.raises(RuntimeError, match="unavailable"):
        supervisor._closure_holds()
    monkeypatch.setitem(
        sys.modules,
        "backlog",
        SimpleNamespace(scoped_blocker_source=lambda: "ok", load_scoped_blockers=lambda: ["O/R#1"]),
    )
    assert supervisor._closure_holds() == {"o/r#1"}
    with pytest.raises(ValueError, match="target"):
        supervisor._repo_num("bad")
    blocked = supervisor.eligibility({"pr_state": "closed", "labels": []})
    assert blocked["eligible"] is False and len(blocked["blockers"]) == 3

    monkeypatch.setattr(
        supervisor.keepalive_shadow, "shadow_decide", lambda _s: {"shadow_action": "wait"}
    )
    monkeypatch.setattr(
        supervisor.keepalive_shadow, "synthesize_report", lambda _s: {"report": "safe"}
    )
    planned = supervisor.plan_for_signals(
        {
            "target": "o/r#1",
            "pr_state": "open",
            "labels": [supervisor.KEEPALIVE_LABEL, "needs-human"],
        },
        report_json_path=tmp_path / "report.json",
    )
    assert planned["next_step"] == "operator_inspect"
    assert planned["proposal_command"] is not None
    assert "--dispatch" in supervisor._historical_collect_command(
        historical_limit=2, backend="codex", include_calibration=True
    )

    signals = tmp_path / "signals.json"
    signals.write_text(
        json.dumps(
            {
                "target": "o/r#1",
                "pr_state": "open",
                "labels": [supervisor.KEEPALIVE_LABEL, "needs-human"],
            }
        )
    )
    assert supervisor.main(["--signals-json", str(signals), "--json"]) == 0
    assert '"next_step": "operator_inspect"' in capsys.readouterr().out
