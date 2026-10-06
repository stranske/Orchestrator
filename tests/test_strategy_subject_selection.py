from __future__ import annotations

import json

import pytest

import strategy_experiment as subject


def _shape(key, rate, *, bad=1, durable=3):
    return {
        "key": key,
        "recurring": True,
        "prs": 4,
        "commit_type": "tests",
        "path_classes": ["tests"],
        "agents": {"codex": {"broke_later_rate": rate, "bad": bad, "durable": durable}},
    }


def test_selects_worst_resolved_rate_and_exact_target(tmp_path):
    path = tmp_path / "shapes.json"
    path.write_text(
        json.dumps({"shapes": [_shape("tests|a|tests", 0.2), _shape("tests|b|tests", 0.5)]})
    )
    issue = {
        "target": "o/r#2",
        "repo": "o/r",
        "title": "tests: add coverage",
        "body": "## Acceptance Criteria\n- pytest coverage test",
    }
    got = subject.select_subject(
        path,
        target="o/r#2",
        issue_fetcher=lambda shape: [issue] if shape["key"] == "tests|b|tests" else [],
    )
    assert got["shape"] == "tests|b|tests"


def test_excludes_open_pr_body_link_and_holders():
    node = {
        "number": 7,
        "timelineItems": {
            "nodes": [
                {
                    "source": {
                        "__typename": "PullRequest",
                        "state": "OPEN",
                        "body": "Fixes #7",
                        "headRefName": "codex/issue-7",
                    }
                }
            ]
        },
    }
    assert subject._open_pr_links_issue(node)
    assert not subject._matches_shape(
        {
            "title": "tests: x",
            "body": "## Acceptance Criteria\n- pytest test",
            "labels": ["needs-human"],
        },
        _shape("x", 0.2),
    )


def test_live_read_failure_is_unknown(monkeypatch):
    def boom(*a, **k):
        raise OSError("offline")

    monkeypatch.setattr("subprocess.run", boom)
    with pytest.raises(RuntimeError, match="UNKNOWN"):
        subject._gh_subjects_for_shape(_shape("x", 0.2) | {"repos": ["o/r"]})


def test_existing_coverage_contract_does_not_require_conventional_title_or_ready_heading():
    issue = {
        "title": "Raise test coverage toward 90%",
        "body": "Every test must FAIL when deliberately broken and PASS after revert. Improve the tracker.",
    }
    assert subject._matches_shape(issue, _shape("tests", 0.33))
    assert not subject._matches_shape(
        {**issue, "labels": ["tracker:durable"]}, _shape("tests", 0.33)
    )
