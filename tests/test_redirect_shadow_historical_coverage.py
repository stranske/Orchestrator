"""Hermetic coverage for RedirectAgent's historical-replay corpus path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import redirect_shadow


@pytest.fixture(autouse=True)
def private_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every corpus and runtime path inside pytest's temporary directory."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path))
    monkeypatch.setattr(redirect_shadow, "CORPUS_PATH", tmp_path / "redirect-shadow.jsonl")


def _append_rows(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_historical_candidates_rank_failures_and_keep_calibration_opt_in(tmp_path: Path) -> None:
    keepalive = tmp_path / "keepalive.jsonl"
    _append_rows(
        keepalive,
        [
            {},
            {
                "target": "owner/repo#success",
                "outcome": "durable",
                "keepalive_blunt": "continue",
                "shadow_action": "inspect",
                "disagreement": True,
                "signals_summary": {},
            },
            {
                "target": "owner/repo#failure",
                "source": "backfill",
                "outcome": "needs_human",
                "keepalive_blunt": "needs-human",
                "shadow_action": "redirect",
                "disagreement": True,
                "signals_summary": {
                    "failure_count": "3",
                    "consecutive_no_progress": "bad",
                    "rounds_without_task_completion": 2,
                },
            },
            {
                "target": "owner/repo#progress",
                "outcome": None,
                "keepalive_blunt": "switch-agent",
                "shadow_action": "inspect",
                "disagreement": False,
                "signals_summary": {"consecutive_no_progress": 3},
            },
        ],
    )

    without_calibration = redirect_shadow.historical_candidates_from_keepalive(
        keepalive_corpus_path=keepalive, limit=1
    )
    assert without_calibration["candidate_count"] == 2
    assert without_calibration["returned"] == 1
    assert without_calibration["missing_target_rows"] == 1
    assert without_calibration["calibration_only_excluded"] == 1
    first = without_calibration["items"][0]
    assert first["target"] == "owner/repo#failure"
    assert first["category"] == "redirect_replay_candidate"
    assert first["priority"] == 275
    assert "historical_final_state_only" in first["reasons"]

    with_calibration = redirect_shadow.historical_candidates_from_keepalive(
        keepalive_corpus_path=keepalive, limit=10, include_calibration=True
    )
    assert [item["target"] for item in with_calibration["items"]] == [
        "owner/repo#failure",
        "owner/repo#progress",
        "owner/repo#success",
    ]


def test_collect_historical_dispatch_is_blinded_and_counterfactual(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keepalive = tmp_path / "keepalive.jsonl"
    corpus = tmp_path / "corpus.jsonl"
    _append_rows(
        keepalive,
        [
            {
                "target": "owner/repo#1",
                "outcome": "reverted",
                "keepalive_blunt": "switch-agent",
                "shadow_action": "inspect",
                "disagreement": True,
                "signals_summary": {"failure_count": 3},
            },
            {
                "target": "owner/repo#2",
                "outcome": "durable",
                "keepalive_blunt": "continue",
                "shadow_action": "inspect",
                "disagreement": True,
                "signals_summary": {},
            },
        ],
    )
    calls: list[dict] = []

    def fake_redirect(report: dict, ac: str, **kwargs: object) -> dict:
        calls.append({"report": report, "ac": ac, **kwargs})
        assert "reverted" not in json.dumps(report)
        return {
            "backend": "cursor",
            "role_run_id": "role:historical:1",
            "decision_source": "redirect_agent",
            "proposal": {"action": "redirect", "corrected_prompt": "retry"},
            "baseline": {"action": "inspect"},
            "plan": {"action": "redirect", "dry_run_only": True},
            "errors": [],
        }

    import roles

    monkeypatch.setattr(roles, "run_redirect_agent", fake_redirect)
    result = redirect_shadow.collect_historical_from_keepalive(
        keepalive_corpus_path=keepalive,
        corpus_path=corpus,
        dispatch=True,
        backend="cursor",
        limit=1,
    )

    assert result["dry_run"] is False
    assert result["collected"] == 1
    assert result["items"][0]["outcome_linked"] is True
    assert calls[0]["source"] == redirect_shadow.HISTORICAL_SOURCE
    events = redirect_shadow._iter_events(corpus)
    assert [event["kind"] for event in events] == [
        "redirect_proposal",
        "redirect_historical_outcome_link",
    ]
    assert events[1]["counterfactual"] is True
    assert events[1]["synced"] is False

    dry = redirect_shadow.collect_historical_from_keepalive(
        keepalive_corpus_path=keepalive, corpus_path=corpus, limit=1
    )
    assert dry["already_replayed"] == ["owner/repo#1"]
    assert dry["would_collect"] == 0


def test_historical_report_outcomes_and_cli_are_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    candidate = {
        "target": "owner/repo#4",
        "watch_state": "unknown",
        "keepalive_blunt": "needs-human",
        "reasons": ["failure_outcome:needs_human", "failure_count:3"],
        "signals_summary": {
            "failure_count": 3,
            "consecutive_no_progress": 0,
            "rounds_without_task_completion": 2,
        },
    }
    report = redirect_shadow._historical_report_from_candidate(candidate)
    assert report["state"] == "running"
    assert report["recommended_action"] == "inspect"
    assert report["drift"]["severity"] == "medium"
    assert "needs_human" not in report["log_tail"]
    assert (
        redirect_shadow._historical_outcome_from_candidate(
            {"target": "owner/repo#4", "outcome": "reverted"}
        )["success"]
        is False
    )

    keepalive = tmp_path / "keepalive.jsonl"
    _append_rows(
        keepalive,
        [
            {
                "target": "owner/repo#5",
                "outcome": "needs_human",
                "keepalive_blunt": "needs-human",
                "shadow_action": "redirect",
                "signals_summary": {"failure_count": 3},
            }
        ],
    )
    corpus = tmp_path / "cli-corpus.jsonl"
    assert (
        redirect_shadow.main(["historical-candidates", "--keepalive-corpus", str(keepalive)]) == 0
    )
    assert json.loads(capsys.readouterr().out)["candidate_count"] == 1
    assert (
        redirect_shadow.main(
            ["collect-historical", "--keepalive-corpus", str(keepalive), "--corpus", str(corpus)]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["would_collect"] == 1
    assert redirect_shadow.main(["summarize", "--corpus", str(corpus)]) == 0
    assert json.loads(capsys.readouterr().out)["n"] == 0
