"""Hermetic coverage for RedirectAgent helpers, outcome lookup, and CLI edges.

Issue #829 ranked `src/redirect_shadow.py` first (143 uncovered units in combined CI).
Prior round closed the historical-replay path; these tests close remaining helper/CLI
branches Gate still reports missing after that commit.
"""

from __future__ import annotations

import hashlib
import io
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import redirect_shadow


@pytest.fixture(autouse=True)
def private_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.setattr(redirect_shadow, "CORPUS_PATH", tmp_path / "redirect-shadow.jsonl")


def test_text_and_json_helpers_cover_none_empty_and_path_reads(tmp_path: Path) -> None:
    assert redirect_shadow._sha_text(None) is None
    assert redirect_shadow._sha_text("abc") == hashlib.sha256(b"abc").hexdigest()

    assert redirect_shadow._hash_history(None) is None
    assert redirect_shadow._hash_history([]) is None
    hist = [{"a": 1}]
    assert redirect_shadow._hash_history(hist) == redirect_shadow._sha_json(hist)

    assert redirect_shadow._short_text(None) is None
    assert redirect_shadow._short_text("") is None
    assert redirect_shadow._short_text("  ") == ""
    assert redirect_shadow._short_text("short") == "short"
    long = "x" * 1500
    assert redirect_shadow._short_text(long, limit=100) == long[-100:]

    payload = {"k": "v"}
    path = tmp_path / "obj.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert redirect_shadow._read_json(str(path)) == payload

    ac_path = tmp_path / "ac.txt"
    ac_path.write_text("from-file", encoding="utf-8")
    assert redirect_shadow._read_text_arg("ignored", str(ac_path)) == "from-file"
    assert redirect_shadow._read_text_arg("inline", "") == "inline"
    assert redirect_shadow._read_text_arg("", "") == ""


def test_read_json_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        redirect_shadow.sys,
        "stdin",
        io.StringIO(json.dumps({"from": "stdin"})),
    )
    assert redirect_shadow._read_json("") == {"from": "stdin"}


def test_iter_events_skips_blank_and_invalid_json(tmp_path: Path) -> None:
    corpus = tmp_path / "c.jsonl"
    corpus.write_text(
        "\n"
        + json.dumps({"kind": "ok", "n": 1})
        + "\n"
        + "{not-json\n"
        + json.dumps(["not", "a", "dict"])
        + "\n"
        + json.dumps({"kind": "ok", "n": 2})
        + "\n",
        encoding="utf-8",
    )
    rows = redirect_shadow._iter_events(corpus)
    assert [r["n"] for r in rows] == [1, 2]
    assert redirect_shadow._iter_events(tmp_path / "missing.jsonl") == []


def test_outcome_for_run_handles_empty_id_db_error_and_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert redirect_shadow._outcome_for_run(None) is None
    assert redirect_shadow._outcome_for_run("") is None

    @contextmanager
    def boom():
        raise RuntimeError("db down")
        yield  # pragma: no cover

    monkeypatch.setattr(redirect_shadow.feedback, "_conn", boom)
    assert redirect_shadow._outcome_for_run("run:missing") is None

    class Conn:
        def execute(self, *_a, **_k):  # noqa: ANN001
            return SimpleNamespace(fetchone=lambda: None)

    @contextmanager
    def empty_conn():
        yield Conn()

    monkeypatch.setattr(redirect_shadow.feedback, "_conn", empty_conn)
    assert redirect_shadow._outcome_for_run("run:empty") is None

    class HitConn:
        def execute(self, *_a, **_k):  # noqa: ANN001
            return SimpleNamespace(
                fetchone=lambda: (
                    "PASS",
                    "PASS",
                    1,
                    "success",
                    "durable",
                    1.0,
                    "ok",
                    None,
                )
            )

    @contextmanager
    def hit_conn():
        yield HitConn()

    monkeypatch.setattr(redirect_shadow.feedback, "_conn", hit_conn)
    monkeypatch.setattr(redirect_shadow.feedback, "_is_success", lambda *a: True)
    out = redirect_shadow._outcome_for_run("run:hit")
    assert out is not None
    assert out["success"] is True
    assert out["merged"] is True
    assert out["durability"] == "durable"


def test_historical_candidates_skips_non_dict_rows(tmp_path: Path) -> None:
    keepalive = tmp_path / "k.jsonl"
    keepalive.write_text(
        "\n".join(
            [
                json.dumps(["not", "dict"]),
                json.dumps(
                    {
                        "target": "owner/repo#x",
                        "outcome": "needs_human",
                        "keepalive_blunt": "needs-human",
                        "shadow_action": "redirect",
                        "signals_summary": {"failure_count": 3},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    result = redirect_shadow.historical_candidates_from_keepalive(
        keepalive_corpus_path=keepalive, limit=5
    )
    assert result["candidate_count"] == 1
    assert result["items"][0]["target"] == "owner/repo#x"


def test_historical_report_state_and_outcome_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    stalled = redirect_shadow._historical_report_from_candidate(
        {
            "target": "o/r#1",
            "watch_state": "stalled",
            "keepalive_blunt": "continue",
            "reasons": ["consecutive_no_progress:4"],
            "signals_summary": {"consecutive_no_progress": 4, "failure_count": 1},
        }
    )
    assert stalled["state"] == "stalled"
    assert stalled["recommended_action"] == "inspect"
    assert any(h["kind"] == "no_progress_rounds" for h in stalled["hints"])

    exited = redirect_shadow._historical_report_from_candidate(
        {
            "target": "o/r#2",
            "watch_state": "exited",
            "reasons": [],
            "signals_summary": {},
        }
    )
    assert exited["state"] == "exited"
    assert exited["recommended_action"] == "collect"

    progress = redirect_shadow._historical_report_from_candidate(
        {
            "target": "o/r#3",
            "watch_state": "",
            "reasons": [],
            "signals_summary": {
                "failure_count": 0,
                "consecutive_no_progress": 0,
                "rounds_without_task_completion": 0,
            },
        }
    )
    assert progress["state"] == "progress"
    assert progress["recommended_action"] == "wait"

    inferred_stall = redirect_shadow._historical_report_from_candidate(
        {
            "target": "o/r#4",
            "watch_state": "weird",
            "reasons": [],
            "signals_summary": {"consecutive_no_progress": 5},
        }
    )
    assert inferred_stall["state"] == "stalled"

    monkeypatch.setattr(redirect_shadow, "_latest_keepalive_outcome_for_target", lambda t: None)
    assert redirect_shadow._historical_outcome_from_candidate({"target": "o/r#5"}) is None
    durable = redirect_shadow._historical_outcome_from_candidate(
        {"target": "o/r#6", "outcome": "durable"}
    )
    assert durable["success"] is True
    assert durable["durability"] == "durable"
    merged = redirect_shadow._historical_outcome_from_candidate(
        {"target": "o/r#7", "outcome": "merged"}
    )
    assert merged["merged"] is True
    assert merged["durability"] == "pending"
    fail = redirect_shadow._historical_outcome_from_candidate(
        {"target": "o/r#8", "outcome": "closed_unmerged"}
    )
    assert fail["success"] is False
    unknown = redirect_shadow._historical_outcome_from_candidate(
        {"target": "o/r#9", "outcome": "mystery"}
    )
    assert unknown["success"] is None
    assert unknown["durability"] == "pending"

    live = {
        "run_id": "keepalive:1",
        "target": "o/r#10",
        "success": True,
        "durability": "durable",
    }
    monkeypatch.setattr(redirect_shadow, "_latest_keepalive_outcome_for_target", lambda t: live)
    assert (
        redirect_shadow._historical_outcome_from_candidate(
            {"target": "o/r#10", "outcome": "needs_human"}
        )
        is live
    )


def test_latest_keepalive_outcome_for_target_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    assert redirect_shadow._latest_keepalive_outcome_for_target("") is None

    @contextmanager
    def boom():
        raise OSError("no db")
        yield  # pragma: no cover

    monkeypatch.setattr(redirect_shadow.feedback, "_conn", boom)
    assert redirect_shadow._latest_keepalive_outcome_for_target("o/r#1") is None

    class Empty:
        def execute(self, *_a, **_k):  # noqa: ANN001
            return SimpleNamespace(fetchone=lambda: None)

    @contextmanager
    def empty():
        yield Empty()

    monkeypatch.setattr(redirect_shadow.feedback, "_conn", empty)
    assert redirect_shadow._latest_keepalive_outcome_for_target("o/r#2") is None

    class Hit:
        def execute(self, *_a, **_k):  # noqa: ANN001
            return SimpleNamespace(
                fetchone=lambda: ("keepalive:9", "PASS", "PASS", 0, "ok", "durable", "n")
            )

    @contextmanager
    def hit():
        yield Hit()

    monkeypatch.setattr(redirect_shadow.feedback, "_conn", hit)
    monkeypatch.setattr(redirect_shadow.feedback, "_is_success", lambda *a: True)
    row = redirect_shadow._latest_keepalive_outcome_for_target("o/r#3")
    assert row["run_id"] == "keepalive:9"
    assert row["merged"] is False
    assert row["success"] is True


def test_main_record_and_link_outcome_cli_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    report = {"target": "owner/repo#cli", "state": "progress", "recommended_action": "wait"}
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    proposal = {"action": "inspect", "confidence": 0.5, "reason": "ok"}
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
    history_path = tmp_path / "history.json"
    history_path.write_text(json.dumps([{"step": 1}]), encoding="utf-8")
    ac_path = tmp_path / "ac.md"
    ac_path.write_text("- [ ] done", encoding="utf-8")
    corpus = tmp_path / "corpus.jsonl"

    calls: list[dict] = []

    def fake_record(report_arg, ac, **kwargs):  # noqa: ANN001
        calls.append({"report": report_arg, "ac": ac, **kwargs})
        entry = {
            "entry_id": "redirect-shadow:cli:1",
            "backend": "cursor",
            "proposal_action": "inspect",
            "baseline_action": "wait",
        }
        return {"entry": entry, "recorded": True, "corpus": str(corpus)}

    monkeypatch.setattr(redirect_shadow, "record_redirect", fake_record)

    with pytest.raises(SystemExit):
        redirect_shadow.main(["record", "--report-json", str(report_path)])

    assert (
        redirect_shadow.main(
            [
                "record",
                "--report-json",
                str(report_path),
                "--proposal-json",
                str(proposal_path),
                "--attempt-history-json",
                str(history_path),
                "--ac-file",
                str(ac_path),
                "--corpus",
                str(corpus),
            ]
        )
        == 0
    )
    text = capsys.readouterr().out
    assert "recorded redirect-shadow:cli:1" in text
    assert "backend=cursor" in text
    assert calls[-1]["report"]["target"] == "owner/repo#cli"
    assert "done" in calls[-1]["ac"]
    assert calls[-1]["proposal_json"] == proposal
    assert calls[-1]["attempt_history"] == [{"step": 1}]

    assert (
        redirect_shadow.main(
            [
                "record",
                "--report-json",
                str(report_path),
                "--proposal-json",
                str(proposal_path),
                "--ac",
                "inline-ac",
                "--corpus",
                str(corpus),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["entry"]["entry_id"] == "redirect-shadow:cli:1"
    assert calls[-1]["ac"] == "inline-ac"
    assert calls[-1]["attempt_history"] is None

    linked = {"event": {"kind": "redirect_outcome_link"}, "recorded": True}

    def fake_link(role_run_id, influenced_run_id, **kwargs):  # noqa: ANN001
        assert role_run_id == "role:1"
        assert influenced_run_id == "run:2"
        assert kwargs["accepted"] is False
        assert kwargs["notes"] == "n"
        assert kwargs["entry_id"] == "e1"
        return linked

    monkeypatch.setattr(redirect_shadow, "link_outcome", fake_link)
    assert (
        redirect_shadow.main(
            [
                "link-outcome",
                "--role-run-id",
                "role:1",
                "--influenced-run-id",
                "run:2",
                "--not-accepted",
                "--notes",
                "n",
                "--entry-id",
                "e1",
                "--corpus",
                str(corpus),
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == linked

    assert (
        redirect_shadow.main(
            [
                "link-outcome",
                "--role-run-id",
                "role:1",
                "--influenced-run-id",
                "run:2",
                "--not-accepted",
                "--notes",
                "n",
                "--entry-id",
                "e1",
                "--corpus",
                str(corpus),
            ]
        )
        == 0
    )
    assert "redirect_outcome_link" in capsys.readouterr().out
