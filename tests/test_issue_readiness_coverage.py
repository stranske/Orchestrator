"""Hermetic coverage for issue-readiness GitHub boundary behavior."""

from __future__ import annotations

import json
import subprocess
import sys
import types

import pytest

import issue_readiness as readiness


def _result(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))


def test_github_read_helpers_are_hermetic_and_surface_failures(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if (
            argv[:2] == ["gh", "issue"]
            and "--json" in argv
            and "number,title,labels,body,author" in argv
            and argv[argv.index("--repo") + 1] == "stranske/One"
        ):
            return _result(stdout='[{"number": 7}]')
        return _result(1, stderr="offline")

    monkeypatch.setattr(readiness.backlog, "SUPPORTED_REPOS", ("stranske/One", "stranske/Two"))
    monkeypatch.setattr(readiness.subprocess, "run", fake_run)

    assert readiness._gh_json(["issue", "list"]) == []
    rows = readiness.fetch_open_issues(limit=3)
    assert rows == [{"number": 7, "repository": {"name": "One"}}]
    listings = [argv for argv, _ in calls if "--repo" in argv]
    assert sorted(argv[argv.index("--repo") + 1] for argv in listings) == [
        "stranske/One",
        "stranske/Two",
    ]
    assert all(argv[argv.index("--limit") + 1] == "3" for argv in listings)
    assert readiness.fetch_failures(limit=3) == ["stranske/One: offline", "stranske/Two: offline"]
    assert all(call[1]["capture_output"] and call[1]["text"] for call in calls)


def test_readiness_writers_report_each_result_without_network(monkeypatch, tmp_path):
    calls = []
    outcomes = iter(
        [
            _result(stdout='[{"name": "status: ready"}]'),
            _result(),
            _result(1, stderr="denied"),
            _result(1, stderr="not found"),
            _result(),
            _result(),
        ]
    )

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return next(outcomes)

    monkeypatch.setattr(readiness.subprocess, "run", fake_run)
    cache = {}
    assert readiness.repo_ready_label("One", cache) == "status: ready"
    assert readiness.repo_ready_label("One", cache) == "status: ready"

    feedback_calls = []
    monkeypatch.setitem(
        sys.modules,
        "feedback",
        types.SimpleNamespace(
            record_owner_question=lambda *args, **kwargs: feedback_calls.append((args, kwargs))
            or {"deduped": False}
        ),
    )
    monkeypatch.setattr(readiness, "repo_ready_label", lambda _: "status: ready")
    applied = readiness.apply_ready(
        [
            {"verdict": "auto_ready", "target": "One#7", "title": "ready"},
            {"verdict": "owner_review", "target": "Two#8", "title": "risk"},
        ],
        dry_run=False,
    )
    assert applied == {
        "applied": ["One#7"],
        "questions_raised": ["Two#8"],
        "errors": [],
        "dry_run": False,
    }
    assert len(feedback_calls) == 1
    question_kwargs = feedback_calls[0][1]
    assert question_kwargs["target"] == "Two#8"
    assert question_kwargs["repo"] == "Two"

    reconciled = readiness.reconcile_durable([{"target": "One#9"}], dry_run=False)
    assert reconciled == {"applied": [], "errors": ["One#9: denied"], "dry_run": False}
    labelled = readiness.apply_task_labels(
        [{"target": "One#10", "label": "refactor"}], dry_run=False
    )
    assert labelled == {
        "applied": ["One#10"],
        "labels_created": ["stranske/One:refactor"],
        "errors": [],
        "dry_run": False,
    }
    edits = [argv for argv, _ in calls if argv[:3] == ["gh", "issue", "edit"]]
    assert [
        "gh",
        "issue",
        "edit",
        "7",
        "--repo",
        "stranske/One",
        "--add-label",
        "status: ready",
    ] in edits
    assert [
        "gh",
        "issue",
        "edit",
        "10",
        "--repo",
        "stranske/One",
        "--add-label",
        "refactor",
    ] in edits
    task_edits = [argv for argv in edits if argv[3] == "10"]
    assert [argv[-1] for argv in task_edits] == ["refactor", "refactor"]
    creates = [argv for argv, _ in calls if argv[:3] == ["gh", "label", "create"]]
    assert [argv[3:6] for argv in creates] == [["refactor", "--repo", "stranske/One"]]
    assert all(call[1]["capture_output"] and call[1]["text"] for call in calls)


def test_input_census_task_labels_and_cli_refusals(tmp_path, monkeypatch, capsys):
    source = tmp_path / "issues.json"
    source.write_text(json.dumps([{"number": 1, "title": "legacy removal", "labels": []}]))
    assert readiness.load_input_issues(source)[0]["number"] == 1
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{}")
    with pytest.raises(ValueError, match="JSON list"):
        readiness.load_input_issues(malformed)

    issues = [
        {"number": 1, "title": "Agent metrics", "labels": ["tracker:durable"]},
        {"number": 2, "title": "Legacy removal", "labels": ["tracker:durable"]},
        {"number": 3, "title": "legacy removal", "labels": []},
    ]
    census = readiness.census(issues)
    assert census["durable"] == 3
    census["labelled_but_counted"] = [{"target": "?#2", "title": "Legacy removal"}]
    assert "Labelled `tracker:durable` but still counted" in readiness.format_census(census)
    assert readiness.task_label_gaps([{"number": 3, "title": "legacy removal", "labels": []}]) == [
        {
            "target": "?#3",
            "title": "legacy removal",
            "label": "refactor",
            "routes_from": "implement",
            "routes_to": "codemod",
        }
    ]

    assert readiness.main(["--input", str(source), "--apply"]) == 2
    assert "refusing --input" in capsys.readouterr().err
    monkeypatch.setattr(readiness, "fetch_open_issues", lambda _limit: [])
    monkeypatch.setattr(readiness, "fetch_failures", lambda: ["stranske/One: offline"])
    assert readiness.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["unreadable_repos"] == ["stranske/One: offline"]
