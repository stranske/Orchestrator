"""#932 operational pass number: ≥4 exploration switches after hosted-challenger deploy."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

import exploration_review
import feedback
import route_weights_export as export
import switch_review


def _brain(path, rows):
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE agent_switches ("
            "pr_ref TEXT, from_agent TEXT, to_agent TEXT, switched_ts INTEGER, "
            "recorded_ts INTEGER, source TEXT, delegation_source TEXT)"
        )
        connection.executemany(
            "INSERT INTO agent_switches VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        connection.commit()
    finally:
        connection.close()


def test_record_deploy_stamps_once_only_when_challengers_are_runnable(tmp_path):
    document = {"exploration": {"challengers": {"implement": "cursor", "review": "codex"}}}
    first = export.record_hosted_exploration_deploy(document, tmp_path, now=1_700_000_000)
    assert first["deploy_ts"] == 1_700_000_000
    assert first["check_date"] == export.HOSTED_EXPLORATION_PASS_CHECK_LABEL
    second = export.record_hosted_exploration_deploy(
        {"exploration": {"challengers": {"implement": "codex"}}},
        tmp_path,
        now=1_700_000_100,
    )
    assert second["deploy_ts"] == 1_700_000_000
    assert export.hosted_exploration_deploy_ts(tmp_path) == 1_700_000_000
    assert (
        export.record_hosted_exploration_deploy(
            {"exploration": {"challengers": {"implement": "gemini"}}},
            tmp_path,
            now=1_700_000_200,
        )
        is None
    )


def test_pass_number_pending_before_check_date_reports_live_count(tmp_path, monkeypatch):
    database = tmp_path / "brain.db"
    deploy = export.HOSTED_EXPLORATION_PASS_CHECK_TS - 14 * 86400
    _brain(
        database,
        [
            ("o/r#1", "codex", "cursor", deploy + 1, deploy, "policy", "route_weights_exploration"),
            ("o/r#2", "codex", "cursor", deploy + 2, deploy, "label", "route_weights_exploration"),
            ("o/r#3", "codex", "cursor", deploy + 3, deploy, "policy", "route_weights"),
            ("o/r#4", "codex", "cursor", deploy - 1, deploy, "policy", "route_weights_exploration"),
        ],
    )
    monkeypatch.setattr(feedback, "DB_PATH", database)
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    export.record_hosted_exploration_deploy(
        {"exploration": {"challengers": {"implement": "cursor"}}},
        tmp_path,
        now=deploy,
    )
    before = export.hosted_exploration_pass_number(
        db_path=database,
        state_dir=tmp_path,
        now=export.HOSTED_EXPLORATION_PASS_CHECK_TS - 1,
    )
    assert before["status"] == "pending"
    assert before["switches"] == 2
    assert before["deploy_ts"] == deploy


@pytest.mark.parametrize(
    ("count", "status"),
    [(3, "fail"), (4, "pass"), (5, "pass")],
    ids=["below-min", "at-min", "above-min"],
)
def test_pass_number_grades_after_check_date(tmp_path, monkeypatch, count, status):
    database = tmp_path / "brain.db"
    deploy = 1_700_000_000
    rows = [
        (
            f"o/r#{index}",
            "codex",
            "cursor",
            deploy + index,
            deploy,
            "policy",
            "route_weights_exploration",
        )
        for index in range(1, count + 1)
    ]
    _brain(database, rows)
    monkeypatch.setattr(feedback, "DB_PATH", database)
    grade = export.hosted_exploration_pass_number(
        db_path=database,
        state_dir=tmp_path,
        deploy_ts=deploy,
        now=export.HOSTED_EXPLORATION_PASS_CHECK_TS,
    )
    assert grade["status"] == status
    assert grade["switches"] == count
    assert grade["min_switches"] == 4


def test_pass_number_unmeasured_without_deploy_or_brain_after_check_date(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "absent.db")
    missing_deploy = export.hosted_exploration_pass_number(
        state_dir=tmp_path,
        now=export.HOSTED_EXPLORATION_PASS_CHECK_TS,
    )
    assert missing_deploy["status"] == "unmeasured"
    assert "deploy stamp absent" in missing_deploy["reason"]

    export.record_hosted_exploration_deploy(
        {"exploration": {"challengers": {"implement": "cursor"}}},
        tmp_path,
        now=1_700_000_000,
    )
    missing_brain = export.hosted_exploration_pass_number(
        db_path=tmp_path / "absent.db",
        state_dir=tmp_path,
        now=export.HOSTED_EXPLORATION_PASS_CHECK_TS,
    )
    assert missing_brain["status"] == "unmeasured"
    assert "unreadable" in missing_brain["reason"]


def test_switch_review_wires_and_renders_hosted_exploration_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "absent.db")
    monkeypatch.setattr(
        switch_review,
        "_gh_call",
        lambda *a, **kw: (True, json.dumps({"exploration": {"rate": 0.2}}), ""),
    )
    monkeypatch.setattr(exploration_review, "build_report", lambda **kw: {"status": "ok"})
    real = export.hosted_exploration_pass_number

    def _pinned(**kwargs):
        kwargs.setdefault("now", export.HOSTED_EXPLORATION_PASS_CHECK_TS - 1)
        return real(**kwargs)

    monkeypatch.setattr(export, "hosted_exploration_pass_number", _pinned)
    (tmp_path / "exploration-window.json").write_text(
        json.dumps(
            {
                "start": "2026-10-17",
                "end": "20261117",
                "extended": False,
                "n_by_policy": {"epsilon-greedy": 0, "thompson-hybrid": 0},
                "decided_at": datetime(2026, 10, 18, tzinfo=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            }
        )
    )
    gate = switch_review._exploration_gate()
    assert gate["hosted_exploration_pass"]["status"] == "pending"
    assert gate["hosted_exploration_pass"]["check_date"] == "2026-11-08"
    rendered = switch_review.format_report(
        {
            "review_days": 7,
            "raise_count": 0,
            "exploration_gate": gate,
            "held_off": [],
            "on_but_idle": [],
            "unconditioned": [],
        }
    )
    assert "#932 hosted-exploration pass: pending" in rendered
    assert "check=2026-11-08" in rendered
