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
    monkeypatch.setattr(sys, "argv", list(sys.argv))

    def unexpected_process(*args, **kwargs):
        pytest.fail(f"unexpected process start: {args!r}")

    monkeypatch.setattr(supervisor.subprocess, "run", unexpected_process)
    monkeypatch.setattr(supervisor.subprocess, "Popen", unexpected_process)


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


def test_panel_adjudication_recording_and_cli_paths(monkeypatch, tmp_path, capsys):
    spec = runtime_ac._valid_spec()
    gate = runtime_ac.evaluate_results(spec, {"check_results": panel._all_check_results(spec)})

    recorded_calls = []

    def mock_record_outcome(run_id, verifier_verdict, notes):
        recorded_calls.append(("record_outcome", run_id, verifier_verdict, notes))
        return {}

    def mock_record_evidence_gap(run_id, reviewer, gap):
        recorded_calls.append(("record_evidence_gap", run_id, reviewer, gap))
        return {}

    def mock_record_evidence_type_citations(names):
        recorded_calls.append(("record_evidence_type_citations", names))
        return names

    monkeypatch.setattr(feedback, "record_outcome", mock_record_outcome)
    monkeypatch.setattr(feedback, "record_evidence_gap", mock_record_evidence_gap)
    monkeypatch.setattr(
        feedback, "record_evidence_type_citations", mock_record_evidence_type_citations
    )

    reviews_pass = {
        "reviews": [
            {
                "reviewer": "vibe",
                "verdict": "PASS",
                "confidence": 0.8,
                "ac_assessments": [
                    {"ac_id": "AC1", "status": "PASS", "reason": "evidence present"}
                ],
            },
            {
                "reviewer": "gemini",
                "verdict": "PASS",
                "confidence": 0.9,
                "ac_assessments": [
                    {"ac_id": "AC2", "status": "PASS", "reason": "evidence present"}
                ],
            },
        ]
    }

    panel_result = panel.adjudicate_panel(spec, gate, reviews_pass)
    assert panel_result["verdict"] == "PASS"
    assert panel_result["verifier_verdict"] == "PASS_RUNTIME_AC"

    run_id = "test-run-1232-adjudication"

    # First recording phase - clear spies before
    recorded_calls.clear()
    record_result = panel.record_panel_verdict(run_id, panel_result)
    assert record_result["run_id"] == run_id
    assert record_result["verifier_verdict"] == "PASS_RUNTIME_AC"
    assert record_result["recorded"] is True
    assert len(recorded_calls) >= 1
    assert any(call[0] == "record_outcome" for call in recorded_calls)
    # Assert exact outcome tuple
    outcome_calls = [c for c in recorded_calls if c[0] == "record_outcome"]
    assert len(outcome_calls) == 1
    assert outcome_calls[0] == (
        "record_outcome",
        run_id,
        "PASS_RUNTIME_AC",
        f"runtime_ac_panel: PASS_RUNTIME_AC - {panel_result['reason']}",
    )

    # Second recording phase - clear spies before
    recorded_calls.clear()
    record_result_gaps = panel.record_panel_verdict(
        run_id,
        {
            **panel_result,
            "evidence_gaps": [
                {"reviewer": "vibe", "gap": "missing trace"},
                {"reviewer": "gemini", "gap": "missing logs"},
            ],
            "evidence_type_citations": [{"name": "trace"}, {"name": "log"}],
        },
    )
    assert record_result_gaps["evidence_gaps"] == 2
    assert any(call[0] == "record_evidence_gap" for call in recorded_calls)
    assert any(call[0] == "record_evidence_type_citations" for call in recorded_calls)

    # Assert ordered gap tuples with runtime_ac_panel reviewer prefixes
    gap_calls = [c for c in recorded_calls if c[0] == "record_evidence_gap"]
    assert len(gap_calls) == 2
    # Gaps should be ordered as provided
    assert gap_calls[0] == ("record_evidence_gap", run_id, "runtime_ac_panel:vibe", "missing trace")
    assert gap_calls[1] == (
        "record_evidence_gap",
        run_id,
        "runtime_ac_panel:gemini",
        "missing logs",
    )

    # Assert exact citation names
    citation_calls = [c for c in recorded_calls if c[0] == "record_evidence_type_citations"]
    assert len(citation_calls) == 1
    assert citation_calls[0][1] == ["trace", "log"]

    spec_file = tmp_path / "spec.json"
    gate_file = tmp_path / "gate.json"
    reviews_file = tmp_path / "reviews.json"
    spec_file.write_text(json.dumps(spec))
    gate_file.write_text(json.dumps(gate))
    reviews_file.write_text(json.dumps(reviews_pass))

    sys.argv = [
        "runtime_ac_panel",
        "--adjudicate",
        str(spec_file),
        "--gate",
        str(gate_file),
        "--reviews",
        str(reviews_file),
    ]
    try:
        result = panel.main(
            [
                "--adjudicate",
                str(spec_file),
                "--gate",
                str(gate_file),
                "--reviews",
                str(reviews_file),
            ]
        )
        assert result == 0
    except SystemExit as e:
        assert e.code == 0

    capsys.readouterr()

    sys.argv = ["runtime_ac_panel", "--prompt", str(spec_file), "--gate", str(gate_file)]
    try:
        result = panel.main(
            ["--prompt", str(spec_file), "--gate", str(gate_file), "--reviewer", "test-reviewer"]
        )
        assert result == 0
    except SystemExit as e:
        assert e.code == 0
    output = capsys.readouterr().out
    assert "STRICT JSON" in output
    assert '"reviewer": "test-reviewer"' in output

    def fake_offload(agent, prompt, **kwargs):
        return {"agent": agent, "exit": 0, "output": '{"verdict":"PASS"}'}

    monkeypatch.setattr(
        panel,
        "dispatch_reviewers",
        lambda *args, **kwargs: {
            "review_doc": reviews_pass,
            "dispatches": [{"agent": "vibe", "parsed": True}],
        },
    )

    # Third recording phase - clear spies before dispatch
    recorded_calls.clear()
    try:
        result = panel.main(
            [
                "--dispatch",
                str(spec_file),
                "--gate",
                str(gate_file),
                "--reviewers",
                "vibe",
                "--record-run-id",
                run_id,
            ]
        )
        assert result == 0
    except SystemExit as e:
        assert e.code == 0

    # Assert dispatch creates its own expected outcome
    dispatch_outcome_calls = [c for c in recorded_calls if c[0] == "record_outcome"]
    assert len(dispatch_outcome_calls) == 1
    assert dispatch_outcome_calls[0][1] == run_id
    assert dispatch_outcome_calls[0][2] == "PASS_RUNTIME_AC"
    assert "runtime_ac_panel: PASS_RUNTIME_AC" in dispatch_outcome_calls[0][3]


def test_runtime_ac_panel_coerce_reviews_and_edge_cases(monkeypatch):
    spec = runtime_ac._valid_spec()
    gate = runtime_ac.evaluate_results(spec, {"check_results": panel._all_check_results(spec)})

    empty_doc = {"reviews": []}
    coerced = panel.coerce_reviews(empty_doc)
    assert coerced == []

    doc_with_invalid = {"reviews": [None, "invalid", {"verdict": "PASS"}]}
    coerced_invalid = panel.coerce_reviews(doc_with_invalid)
    assert len(coerced_invalid) == 3

    # Call adjudicate_panel with doc_with_invalid and require ValueError matching 'must be an object'
    with pytest.raises(ValueError, match="must be an object"):
        panel.adjudicate_panel(spec, gate, doc_with_invalid)

    doc_with_valid = {
        "reviews": [
            {"reviewer": "vibe", "verdict": "PASS"},
            {"reviewer": "gemini", "verdict": "FAIL"},
        ]
    }
    coerced_valid = panel.coerce_reviews(doc_with_valid)
    assert len(coerced_valid) == 2

    panel_result = panel.adjudicate_panel(spec, gate, doc_with_valid)
    assert panel_result["n_reviews"] == 2

    with pytest.raises(ValueError, match="review JSON must be"):
        panel.coerce_reviews("invalid")


def test_relearn_report_build_report_and_ordering(monkeypatch):
    monkeypatch.setattr(relearn_report, "_role_priors_from_registry", lambda _priors: {})

    report = {
        "dry_run": False,
        "learner": "test-learner",
        "window_days": 7,
        "new_version": 2,
        "previous_version": 1,
        "learner_note": "test note",
        "cost_telemetry": {"task1": {"measured": True, "coverage": 0.95}},
        "tasks": [
            {
                "task_type": "task1",
                "diverges_from_prior": False,
                "prior_order": ["agent1", "agent2"],
                "learned_order": ["agent1", "agent2"],
                "previous_order": ["agent1", "agent2"],
                "note": "stable",
                "rows": [
                    {
                        "agent": "agent1",
                        "posterior": 0.8,
                        "score": 0.85,
                        "n_obs": 10,
                        "learned_rank": 1,
                        "prior_rank_delta": 0,
                        "previous_rank_delta": 0,
                    },
                    {
                        "agent": "agent2",
                        "posterior": 0.7,
                        "score": 0.75,
                        "n_obs": 8,
                        "learned_rank": 2,
                        "prior_rank_delta": 0,
                        "previous_rank_delta": 0,
                    },
                ],
            }
        ],
    }

    ordered_rows = relearn_report._ordered_rows(
        [
            {"agent": "agent2", "score": 0.75},
            {"agent": "agent1", "score": 0.85},
        ],
        ["agent1", "agent2"],
    )
    assert ordered_rows[0]["agent"] == "agent1"
    assert ordered_rows[1]["agent"] == "agent2"

    # Prior order opposes both input and alphabetical order.
    equal_score_rows = relearn_report._ordered_rows(
        [
            {"agent": "alpha", "score": 0.85},
            {"agent": "zebra", "score": 0.85},
        ],
        ["zebra", "alpha"],
    )
    assert [row["agent"] for row in equal_score_rows] == ["zebra", "alpha"]

    # Test with equal scores and alphabetical tiebreaker
    # When prior ranks are equal (both not in prior_order), alphabetical order is used
    equal_score_alpha = relearn_report._ordered_rows(
        [
            {"agent": "zebra", "score": 0.85},
            {"agent": "alpha", "score": 0.85},
        ],
        ["agent1", "agent2"],
    )
    assert equal_score_alpha[0]["agent"] == "alpha"
    assert equal_score_alpha[1]["agent"] == "zebra"

    human_output = relearn_report.format_human(report)
    assert "test-learner" in human_output
    assert "window_days=7" in human_output or "7 days" in human_output
    assert "task1" in human_output


def test_keepalive_supervisor_plan_target_and_closure(monkeypatch, tmp_path):
    signals = {
        "target": "stranske/Orchestrator#1234",
        "pr_state": "open",
        "labels": [supervisor.KEEPALIVE_LABEL, "needs-human"],
        "title": "Test issue",
        "body": "Test body",
    }

    monkeypatch.setattr(supervisor.keepalive_shadow, "gather_signals", lambda target: signals)
    monkeypatch.setattr(
        supervisor.keepalive_shadow, "shadow_decide", lambda _signals: {"shadow_action": "wait"}
    )
    monkeypatch.setattr(supervisor, "build_redirect_report", lambda *_args: {"report": "safe"})

    plan = supervisor.plan_for_signals(signals, report_json_path=tmp_path / "report.json")
    assert "next_step" in plan
    assert "proposal_command" in plan
    # Assert operator_inspect, eligible=True, blockers=[], direct report existence
    assert plan["next_step"] == "operator_inspect"
    assert plan["eligible"] is True
    assert plan["eligibility"]["blockers"] == []
    assert plan["report_written"] is not None
    assert (tmp_path / "report.json").exists()
    # Assert the exact --report-json/--ac/--dispatch command suffix
    # Command structure: [..., '--report-json', '<path>', '--ac', '<value>', '--dispatch']
    assert plan["proposal_command"][-5:] == [
        "--report-json",
        str(tmp_path / "report.json"),
        "--ac",
        supervisor.DEFAULT_AC,
        "--dispatch",
    ]

    plan_target_result = supervisor.plan_target(
        signals["target"],
        report_dir=tmp_path,
    )
    assert plan_target_result["next_step"] == "operator_inspect"
    assert plan_target_result["report_written"] is not None
    assert (tmp_path / "stranske__Orchestrator__1234.keepalive-supervisor-report.json").exists()

    eligible = supervisor.eligibility(signals)
    assert "eligible" in eligible
    assert "blockers" in eligible
    assert eligible["eligible"] is True
    assert eligible["blockers"] == []

    label_set = supervisor._label_set(signals)
    assert supervisor.KEEPALIVE_LABEL in label_set


def test_runtime_ac_panel_substantiated_veto_and_normalize(monkeypatch):
    review_pass = {
        "reviewer": "test",
        "verdict": "PASS",
        "confidence": 0.8,
        "rationale": "",
        "ac_assessments": [],
        "blockers": [{"ac_id": "AC1", "severity": "medium", "finding": "issue"}],
        "evidence_gaps": [],
        "cited_evidence_types": [],
        "cited_evidence_contracts": [],
    }
    assert panel._substantiated_veto(review_pass) is False

    review_fail_no_veto = {
        "reviewer": "test",
        "verdict": "FAIL",
        "confidence": 0.8,
        "rationale": "",
        "ac_assessments": [],
        "blockers": [{"ac_id": "AC1", "severity": "low", "finding": "issue"}],
        "evidence_gaps": [],
        "cited_evidence_types": [],
        "cited_evidence_contracts": [],
    }
    assert panel._substantiated_veto(review_fail_no_veto) is False

    review_fail_with_veto = {
        "reviewer": "test",
        "verdict": "FAIL",
        "confidence": 0.8,
        "rationale": "",
        "ac_assessments": [],
        "blockers": [{"ac_id": "AC1", "severity": "high", "finding": "issue"}],
        "evidence_gaps": [],
        "cited_evidence_types": [],
        "cited_evidence_contracts": [],
    }
    assert panel._substantiated_veto(review_fail_with_veto) is True

    assert panel._normalize_status("PASS") == "PASS"
    assert panel._normalize_status("pass") == "PASS"
    assert panel._normalize_status("FAIL") == "FAIL"
    assert panel._normalize_status("invalid") == "NEEDS_REVIEW"
    assert panel._normalize_status(None) == "NEEDS_REVIEW"

    assert panel._clamp_confidence(0.5) == 0.5
    assert panel._clamp_confidence(1.5) == 1.0
    assert panel._clamp_confidence(-0.5) == 0.0
    assert panel._clamp_confidence("0.5") == 0.5
    assert panel._clamp_confidence("invalid") == 0.0


def test_relearn_report_priors_and_blend(monkeypatch):
    monkeypatch.setattr(relearn_report, "_role_priors_from_registry", lambda _priors: {})

    route_table = {
        "task1": {
            "neutral_prior": False,
            "agents": [
                {"agent": "agent1"},
                {"agent": "agent2"},
                {"agent": "agent3"},
            ],
        },
        "task2": {
            "neutral_prior": True,
            "agents": [
                {"agent": "agentA"},
            ],
        },
    }
    priors = relearn_report.priors_from_route_table(route_table)
    assert "task1" in priors
    assert "task2" in priors
    assert priors["task1"]["agent1"] == 0.70
    assert priors["task1"]["agent2"] == 0.65
    assert priors["task1"]["agent3"] == 0.60
    assert priors["task2"]["agentA"] == 0.5

    blended = relearn_report.blend_bt_priors(
        {"task1": {"agent1": 0.7, "agent2": 0.65}},
        bt_fn=lambda **_k: {"ready": False},
    )
    assert blended == {}

    ready_priors = {"task1": {"agent1": 0.7, "agent2": 0.65}}
    blended_ready = relearn_report.blend_bt_priors(
        ready_priors,
        bt_fn=lambda **_k: {
            "ready": True,
            "strengths": {"agent2": 0.9},
            "comparisons": 10,
        },
    )
    assert "task1" in blended_ready
    assert blended_ready["task1"] == 10
    assert ready_priors["task1"]["agent2"] == pytest.approx(0.675)


def test_keepalive_supervisor_repo_num_and_eligibility(monkeypatch):
    repo, num = supervisor._repo_num("stranske/Orchestrator#1334")
    assert repo == "stranske/Orchestrator"
    assert num == "1334"

    with pytest.raises(ValueError, match="target must be"):
        supervisor._repo_num("invalid")

    signals_open = {
        "target": "stranske/Orchestrator#1334",
        "pr_state": "open",
        "labels": [supervisor.KEEPALIVE_LABEL, "needs-human"],
        "title": "Test",
        "body": "Test",
    }
    eligible = supervisor.eligibility(signals_open)
    assert eligible["eligible"] is True
    assert len(eligible["blockers"]) == 0

    signals_closed = {
        "target": "stranske/Orchestrator#1334",
        "pr_state": "closed",
        "labels": [supervisor.KEEPALIVE_LABEL, "needs-human"],
        "title": "Test",
        "body": "Test",
    }
    eligible_closed = supervisor.eligibility(signals_closed)
    assert eligible_closed["eligible"] is False
    assert "pr_not_open" in eligible_closed["blockers"]

    signals_no_label = {
        "target": "stranske/Orchestrator#1334",
        "pr_state": "open",
        "labels": ["needs-human"],
        "title": "Test",
        "body": "Test",
    }
    eligible_no_label = supervisor.eligibility(signals_no_label)
    assert eligible_no_label["eligible"] is False
    assert "missing_agents_keepalive_label" in eligible_no_label["blockers"]


def test_keepalive_supervisor_proposal_backend(monkeypatch, tmp_path):
    signals = {
        "target": "stranske/Orchestrator#1334",
        "pr_state": "open",
        "labels": [supervisor.KEEPALIVE_LABEL, "needs-human"],
    }
    monkeypatch.setattr(
        supervisor.keepalive_shadow, "shadow_decide", lambda _s: {"shadow_action": "wait"}
    )
    monkeypatch.setattr(supervisor, "build_redirect_report", lambda *_a: {"report": "safe"})
    plan = supervisor.plan_for_signals(
        signals, report_json_path=tmp_path / "report.json", proposal_backend="codex"
    )
    assert plan["proposal_command"][-3:] == ["--backend", "codex", "--dispatch"]
    assert plan["stage2_record_command"] == plan["proposal_command"] + ["--record-corpus", "--json"]
