"""A failed Brain read must not masquerade as measured zero exploration runs."""

from __future__ import annotations

import exploration_review
import feedback
import switch_review


def test_db_error_is_named_not_zero(tmp_path, monkeypatch):
    def ready_task(task_type, **_kwargs):
        return {
            "task_type": task_type,
            "agents": ["codex"],
            "zero_observation_agents": [],
            "ready_for_default_review": True,
            "total_observations": 20,
            "observed_agents": 1,
            "exploitation_pick": {"agent": "codex"},
        }

    monkeypatch.setattr(exploration_review, "_task_summary", ready_task)
    route_table = {task: {} for task in ("implement", "testgen", "codemod")}
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    with feedback._conn():
        pass

    empty = exploration_review.build_report(route_table=route_table, version=1, trials=1)
    assert empty["status"] == "direct_mode_evidence_not_ready"
    assert "evidence_error" not in empty
    assert "evidence_error" not in empty["recorded_exploration_evidence"]

    unreadable = tmp_path / "directory-instead-of-brain.db"
    unreadable.mkdir()
    monkeypatch.setattr(feedback, "DB_PATH", unreadable)
    failed = exploration_review.build_report(route_table=route_table, version=1, trials=1)
    assert failed["status"] == "direct_mode_evidence_unreadable"
    assert failed["evidence_error"].startswith("OperationalError:")
    assert failed["drainable"] == "repair the Brain read"
    assert "repair the Brain read" in exploration_review.format_human(failed)

    monkeypatch.setattr(switch_review, "SWITCH_CAPABILITY", {})
    monkeypatch.setattr(switch_review, "_capability_heartbeat", lambda: None)
    monkeypatch.setattr(switch_review, "stale_runners", lambda: [])
    monkeypatch.setattr(switch_review, "mirror_drift", lambda: {"status": "ok"})
    monkeypatch.setattr(switch_review, "fleet_gates", lambda **_kwargs: {"suspect": False})
    sweep = switch_review.review(env={})
    assert sweep["exploration_gate"]["suspect"] is True
    assert sweep["exploration_gate"]["status"] == "direct_mode_evidence_unreadable"
    assert "SUSPECT — direct_mode_evidence_unreadable" in switch_review.format_report(sweep)


def test_unexpected_exploration_report_error_has_distinct_remediation(monkeypatch):
    def fail_report(**_kwargs):
        raise RuntimeError("unexpected report failure")

    monkeypatch.setattr(exploration_review, "build_report", fail_report)
    gate = switch_review._exploration_gate()
    assert gate["status"] == "exploration_report_error"
    assert gate["suspect"] is True
    assert gate["evidence_error"] == "RuntimeError: unexpected report failure"
    assert gate["drainable"] == "repair exploration report generation"
