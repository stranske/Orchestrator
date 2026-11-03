"""Hermetic coverage for redirect_apply's offline and failure-only paths."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import redirect_apply as ra
import redirect_shadow


@pytest.fixture()
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep every stateful helper inside pytest's directory, never the live runtime."""
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    state.mkdir()
    runtime.mkdir()
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(runtime))
    monkeypatch.setattr(redirect_shadow, "CORPUS_PATH", tmp_path / "corpus.jsonl")
    return tmp_path


def test_link_outcomes_dry_run_success_and_failure_are_explicit(isolated_runtime, monkeypatch):
    corpus = isolated_runtime / "corpus.jsonl"
    rows = [
        {
            "role_run_id": "role:redirect:a",
            "influenced_run_id": "run-a",
            "verdict": "PASS",
            "durability": "durable",
        },
        {
            "role_run_id": "role:redirect:b",
            "influenced_run_id": "run-b",
            "verdict": "FAIL",
            "durability": "durable",
        },
    ]
    monkeypatch.setattr(ra, "pending_outcome_links", lambda **_k: rows)
    monkeypatch.setattr(redirect_shadow, "linked_pairs", lambda _path: set())
    dry = ra.preview_link_applied_outcomes(corpus_path=corpus)
    assert dry["pending"] == 2 and dry["linked"] == 0 and len(dry["links"]) == 2

    calls: list[tuple[str, str]] = []

    def link(role_id, run_id, **_kwargs):
        calls.append((role_id, run_id))
        if run_id == "run-b":
            raise RuntimeError("recording failed")
        return {"event": {"link_result": {"synced": True}}}

    beats: list[str] = []
    monkeypatch.setattr(redirect_shadow, "link_outcome", link)
    monkeypatch.setattr(redirect_shadow, "_append_event", lambda event, _path: event)
    monkeypatch.setattr(ra, "_heartbeat", lambda event, **_k: beats.append(event))
    result = ra.link_applied_outcomes(corpus_path=corpus)
    assert calls == [("role:redirect:a", "run-a"), ("role:redirect:b", "run-b")]
    assert result["linked"] == 1 and result["failed"] == 1
    assert result["failures"][0]["error_type"] == "RuntimeError"
    assert beats == ["outcome"]


def test_sweep_and_disk_helpers_reject_bad_or_duplicate_candidates(isolated_runtime):
    sweep = isolated_runtime / "sweep.json"
    sweep.write_text(
        json.dumps(
            {
                "generated_at": 100,
                "actionable": [
                    {"target": "o/r#1", "state": "stalled"},
                    {"target": "o/r#2", "state": "running"},
                    {"state": "stalled"},
                    "not-a-report",
                ],
            }
        )
    )
    rows, meta = ra.sweep_stalled_candidates(sweep, now=101)
    assert meta["status"] == "current" and [row["report"]["target"] for row in rows] == ["o/r#1"]
    sweep.write_text('{"generated_at": 100, "actionable": "not-a-list"}')
    assert ra.sweep_stalled_candidates(sweep, now=101)[1]["status"] == "unreadable"
    sweep.write_text('{"generated_at": 100, "actionable": []}')
    assert ra.sweep_stalled_candidates(sweep, now=100 + ra.DAY_SECONDS + 1)[1]["status"] == "stale"

    report_dir = isolated_runtime / "reports"
    report_dir.mkdir()
    (report_dir / "bad.keepalive-supervisor-report.json").write_text("not json")
    (report_dir / "good.keepalive-supervisor-report.json").write_text('{"target":"o/r#1"}')
    disk = ra._reports_on_disk(report_dir)
    assert [row["report"]["target"] for row in disk] == [
        "bad.keepalive-supervisor-report.json",
        "o/r#1",
    ]
    supervisor = [{"report": {"target": "o/r#1"}}, {"report": {"target": "o/r#1"}}]
    merged, supervisor_count, sweep_count = ra._merge_apply_candidates(
        supervisor, rows + [{"report": {"target": "o/r#3"}}]
    )
    assert [row["report"]["target"] for row in merged] == ["o/r#1", "o/r#3"]
    assert (supervisor_count, sweep_count) == (1, 1)


def test_flag_resolution_and_cli_do_not_need_live_state(isolated_runtime, monkeypatch, capsys):
    monkeypatch.setenv(ra.BOOTSTRAP_FLAG, " 1 ")
    assert ra.flag_as_the_tick_sees_it() == (True, "ambient")
    monkeypatch.delenv(ra.BOOTSTRAP_FLAG)
    monkeypatch.setattr(
        "capability_recurrence_check.tick_env", lambda refresh: {ra.BOOTSTRAP_FLAG: "0"}
    )
    assert ra.flag_as_the_tick_sees_it() == (False, "tick")
    monkeypatch.setattr(ra, "status", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(ra, "format_status", lambda out: ["status ok"])
    assert ra.main(["--status"]) == 0
    assert capsys.readouterr().out.strip() == "status ok"
