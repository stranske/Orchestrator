import json
import socket
import subprocess

import pytest

import feedback
import issue_quality as quality


@pytest.fixture(autouse=True)
def isolated_issue_quality(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    attempted = []

    def forbid_external(*args, **kwargs):
        attempted.append(args)
        raise AssertionError("issue quality tests must not start processes or network calls")

    monkeypatch.setattr(subprocess, "Popen", forbid_external)
    monkeypatch.setattr(socket, "create_connection", forbid_external)
    yield
    assert attempted == []


def _record(run_id, target, durability, *, ts=1000):
    feedback.record_run(
        run_id,
        target,
        "implement",
        "codex",
        mode="remote",
        pr_number=int(target.rsplit("#", 1)[1]),
        source="keepalive",
        assignment="assigned",
        work_type="issue",
        ts=ts,
    )
    feedback.record_outcome(run_id, durability=durability)


def test_selection_and_target_parsing_are_read_only(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    monkeypatch.setattr(feedback, "DB_PATH", db)
    _record("keepalive:o/r#7:codex", "o/r#7", "durable")
    _record("keepalive:o/r#8:codex", "o/r#8", "pending")
    before = db.read_bytes()

    rows = quality.select_completed_keepalive_issue_runs(1, db_path=db, now=1001)

    assert rows == [quality.RunRow("keepalive:o/r#7:codex", 1000, "o/r#7", 7, "durable")]
    assert db.read_bytes() == before
    assert quality._target_parts(rows[0]) == ("o/r", 7)
    assert quality._target_parts({"target": "o/r#9", "pr_number": "9", "run_id": "x"}) == (
        "o/r",
        9,
    )
    assert quality._target_parts({"target": "", "pr_number": 3, "run_id": "keepalive:o/r#3:x"}) == (
        "o/r",
        3,
    )
    assert quality._target_parts({"target": "bad", "run_id": "bad"}) is None


def test_selection_rejects_missing_schema_and_readonly_database(tmp_path):
    with pytest.raises(FileNotFoundError):
        quality._readonly_conn(tmp_path / "missing.db")

    db = tmp_path / "partial.db"
    import sqlite3

    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE runs(run_id TEXT)")
        conn.execute("CREATE TABLE outcomes(run_id TEXT)")
    with pytest.raises(RuntimeError, match="missing required columns"):
        quality.select_completed_keepalive_issue_runs(db_path=db, now=1000)


def test_fetchers_accept_only_well_formed_gh_payloads(monkeypatch):
    calls = []

    def run_json(args, *, timeout=30):
        calls.append((args, timeout))
        return {"closingIssuesReferences": [{"number": "4"}, {}, {"number": 4}, {"number": 5}]}

    monkeypatch.setattr(quality, "_run_json", run_json)
    assert quality.fetch_linked_issue_numbers("o/r", 2) == [4, 5]
    monkeypatch.setattr(quality, "_run_json", lambda *args, **kwargs: {"body": 3})
    assert quality.fetch_issue_body("o/r", 4) == {"body": 3}
    assert calls[0][0][:4] == ["gh", "pr", "view", "2"]
    assert quality._coerce_issue_payload(None) is None
    assert quality._coerce_issue_payload("body") == ("body", None, None)
    assert quality._coerce_issue_payload({"body": "body", "title": "t", "url": "u"}) == (
        "body",
        "t",
        "u",
    )
    assert quality._coerce_issue_payload({"title": "missing"}) is None


def test_resolution_features_analysis_and_rendering_cover_failure_boundaries():
    rows = [
        quality.RunRow("a", 1, "o/r#1", 1, "durable"),
        quality.RunRow("b", 1, "o/r#2", 2, "reverted"),
        quality.RunRow("broken", 1, "bad", None, "durable"),
        quality.RunRow("unlinked", 1, "o/r#3", 3, "durable"),
    ]
    linked = {1: [11, 12], 2: [21], 3: []}
    payloads = {
        11: {
            "body": "## Repro steps\nRun pytest\n\n## Acceptance criteria\n- [ ] verify pass\n"
            "`pkg.module` tests/test_x.py https://github.com/o/r/pull/1",
            "title": "rich",
            "url": "u",
        },
        12: None,
        21: "Maybe this is unclear?",
    }
    records, skipped = quality.resolve_issue_records(
        rows,
        linked_issue_fn=lambda _repo, pr: linked[pr],
        body_fetch_fn=lambda _repo, issue: payloads[issue],
        max_workers=1,
    )

    assert [(r.issue_number, r.durability) for r in records] == [(11, "durable"), (21, "reverted")]
    assert {item["run_id"]: item["reason"] for item in skipped} == {
        "a": "could not fetch issue body",
        "broken": "could not parse repo/pr",
        "unlinked": "no closingIssuesReferences",
    }
    features = quality.extract_issue_features(records[0].body)
    assert features["has_repro_steps"] and features["has_acceptance_criteria"]
    assert features["has_test_instructions"] and features["linked_pr_count"] == 1
    assert quality._heading_present("## Scope\n", "scope")
    assert quality._body_length_bucket(79) == "short"
    assert quality._body_length_bucket(80) == "medium"
    assert quality._body_length_bucket(251) == "long"

    report = quality.analyze_issue_records(records, min_cell_n=1)
    assert report["baseline"] == {"n": 2, "durable": 1, "durable_rate": 0.5}
    assert report["issues"][0]["score"] >= report["issues"][1]["score"]
    report.update(
        {"lookback_days": 90, "runs_selected": 4, "issues_analyzed": 2, "skipped": skipped}
    )
    human = quality.format_human(report)
    assert "Issue quality -> durability associations" in human
    assert "Skipped linked issue/body lookups: 3" in human
    assert quality._pct(None) == "n/a" and quality._num(None) == "n/a"


def test_build_report_and_main_use_injected_offline_readers(monkeypatch, capsys):
    row = quality.RunRow("a", 1, "o/r#1", 1, "durable")
    monkeypatch.setattr(
        quality, "select_completed_keepalive_issue_runs", lambda *args, **kwargs: [row]
    )
    report = quality.build_report(
        linked_issue_fn=lambda *_: [2],
        body_fetch_fn=lambda *_: {"body": "## Tests\npytest"},
        now=2,
    )
    assert report["runs_selected"] == report["issues_analyzed"] == 1
    report_options = {}

    def build_report(**kwargs):
        report_options.update(kwargs)
        return {"answer": 42}

    monkeypatch.setattr(quality, "build_report", build_report)
    assert quality.main(["--json", "--lookback-days", "7", "--workers", "1"]) == 0
    assert json.loads(capsys.readouterr().out) == {"answer": 42}
    assert report_options == {"lookback_days": 7, "min_cell_n": 10, "max_workers": 1}
