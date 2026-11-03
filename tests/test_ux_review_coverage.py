"""Offline coverage for UX-review parsing, archival discovery, and pattern aggregation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import ux_review as ur


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """These pure helpers must never inherit a developer's live state."""
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    state.mkdir()
    runtime.mkdir()
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(runtime))


def test_adversarial_parser_and_gap_normalizer_reject_bad_shapes():
    assert ur.parse_adversarial_output("") is None
    assert ur.parse_adversarial_output("not json")["parse_error"] == "nonempty_unparseable_output"
    assert (
        ur.parse_adversarial_output('{"findings": "bad"}')["parse_error"]
        == "invalid_findings_schema"
    )
    valid = ur.parse_adversarial_output(
        '{"findings":[{"stuck_probability":0.5,"severity":3,"click_path":["go"]}]}'
    )
    assert valid and valid["findings"][0]["severity"] == 3
    assert ur._normalize_evidence_gaps(
        {"evidence_gaps": ["  Missing  label ", "missing label", {"gap": ""}]}
    ) == ["Missing label"]


def test_discovery_backfill_skips_fixture_no_arm_and_mixed_spec_panels(tmp_path: Path):
    good = tmp_path / "owner" / "Repo_uxreview_2026-01-02"
    good.mkdir(parents=True)
    (good / "rubric-prompt-claude.txt").write_text("same rubric")
    (good / "rubric-out-claude.txt").write_text("answer")
    (good / "rubric-out-claude-retry1.txt").write_text("retry")
    (good / "rubric-out-codex.FAILED-http500.txt").write_text("failed")
    no_arm = tmp_path / "_gate2_smoke_uxreview_2026-01-02"
    no_arm.mkdir()
    (no_arm / "rubric-prompt-test.txt").write_text("fixture")
    mixed = tmp_path / "Different_uxreview_2026-01-02"
    mixed.mkdir()
    (mixed / "rubric-prompt-claude.txt").write_text("a")
    (mixed / "rubric-prompt-codex.txt").write_text("b")
    (mixed / "rubric-out-claude.txt").write_text("answer")

    panels = ur.discover_historical_panels(tmp_path)
    assert len(panels) == 2
    repo = next(panel for panel in panels if panel["app"] == "owner/Repo")
    assert repo["arms"] == ["claude"] and repo["base_sha_unrecoverable"] is True
    result = ur.backfill_panel_subjects(tmp_path)
    assert result["registered"] == 1 and result["skipped"][0]["reason"].startswith("rubric differs")
    assert result["without_base_sha_is_permanent_for_discovered_panels"] is True


def test_cross_repo_patterns_filters_low_severity_and_requires_distinct_apps(monkeypatch):
    rows = [
        (
            "o/a",
            "o/a:uxreview:1",
            json.dumps(
                {
                    "findings": [
                        {"failure_mode": "confusion", "severity": 3, "screen": "A", "element": "go"}
                    ]
                }
            ),
        ),
        (
            "o/b",
            "o/b:uxreview:1",
            json.dumps(
                {
                    "findings": [
                        {
                            "failure_mode": "confusion",
                            "severity": 4,
                            "screen": "B",
                            "element": "save",
                        },
                        {"failure_mode": "minor", "severity": 2},
                    ]
                }
            ),
        ),
        ("o/c", "other", "not json"),
    ]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args):
            return self

        def fetchall(self):
            return rows

    monkeypatch.setattr(ur.feedback, "_conn", Connection)
    patterns = ur.cross_repo_patterns(min_recurrence=2)
    assert patterns == [
        {
            "failure_mode": "confusion",
            "category": "confusion",
            "app_count": 2,
            "apps": ["o/a", "o/b"],
            "occurrences": 2,
            "max_severity": 4,
            "examples": [
                {"app": "o/a", "screen": "A", "element": "go", "severity": 3},
                {"app": "o/b", "screen": "B", "element": "save", "severity": 4},
            ],
        }
    ]
