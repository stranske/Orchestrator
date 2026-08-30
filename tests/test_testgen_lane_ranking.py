"""The lane's source selection: what it chooses, and what it does when it cannot choose.

`escaped_defect_priority` could rank files from the day it merged and nothing called it, which in
this repository is the failure mode with its own rule rather than an oversight. These tests pin the
wiring — but mostly they pin the FALLBACKS, because a chooser that silently chooses nothing is
worse than no chooser: an empty source list reads as "no file needs tests", which is the good-news
reading of a failed subprocess.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import escaped_defect_priority as edp
import testgen_lane as tl


def _repo(tmp_path: Path, *, layout: str = "src") -> Path:
    """A real git repository — the parsing and the git probe are the parts that break."""
    (tmp_path / layout).mkdir(parents=True, exist_ok=True)
    (tmp_path / layout / "hot.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / layout / "cold.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "feat: initial"], cwd=tmp_path, check=True)
    (tmp_path / layout / "hot.py").write_text("x = 2\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-aqm", "fix: hot was wrong"], cwd=tmp_path, check=True)
    return tmp_path


@pytest.fixture()
def on(monkeypatch):
    monkeypatch.setenv(tl.RANKING_SWITCH, "1")


@pytest.fixture()
def off(monkeypatch):
    monkeypatch.delenv(tl.RANKING_SWITCH, raising=False)


# --------------------------------------------------------------------------------------------
# What it chooses.
# --------------------------------------------------------------------------------------------


def test_a_file_a_fix_commit_touched_is_chosen_before_one_it_did_not(tmp_path, on):
    sources, ranking, _ = tl.ranked_sources(repo=_repo(tmp_path), limit=2)
    assert sources[0] == "hot"
    assert ranking[0]["path"].endswith("hot.py")


def test_a_source_root_is_stripped_but_a_package_is_kept(tmp_path):
    """`coverage run --source=src/mod.py` measures NOTHING and exits 0 — verified, not assumed.

    So the ranked file path has to become an importable name, and which leading component to drop
    is decided by `__init__.py` rather than by a hardcoded prefix: `src` is a source root in this
    repository and a package in others.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    assert tl._importable_source(tmp_path, "src/mod.py") == "mod"
    assert tl._importable_source(tmp_path, "pkg/mod.py") == "pkg.mod"


def test_the_chosen_sources_reach_the_gate_command(tmp_path, on, capsys):
    """The point of the wiring: what was ranked is what the acceptance gate measures."""
    tl.main(
        [
            "--repo",
            str(_repo(tmp_path)),
            "--rank-sources",
            "1",
            "--baseline-pytest-args",
            "tests",
            "--candidate-pytest-args",
            "tests",
        ]
    )
    assert "--source hot" in capsys.readouterr().out


# --------------------------------------------------------------------------------------------
# The rationale. A stated reason that no input can falsify is worse than no stated reason.
# --------------------------------------------------------------------------------------------


def test_the_rationale_carries_the_real_tier_numbers(tmp_path, on):
    """Regression pin: the first draft read `escaped`/`churn`/`uncovered` with a default of 0.

    `as_dict` emits `tier1_escaped_defects`/`tier2_churn`/`tier3_uncovered_statements`, so every
    key missed and every file's stated reason was "escaped 0, churn 0, uncovered 0" — a correct
    ordering under a rationale no input could ever contradict. Asserting the VALUE rather than the
    label is what makes a key rename break this test instead of silently zeroing the prompt.
    """
    sources, ranking, _ = tl.ranked_sources(repo=_repo(tmp_path), limit=2)
    prompt = tl.build_prompt(
        repo=tmp_path,
        sources=sources,
        baseline_pytest_args="tests",
        candidate_pytest_args="tests",
        ranking=ranking,
    )
    weight = ranking[0]["tier1_escaped_defects"]
    assert weight > 0, "the fixture commits a fix, so tier 1 must be non-zero to pin anything"
    assert f"escaped-defect weight {weight}" in prompt
    assert f"churn {ranking[0]['tier2_churn']}" in prompt


def test_an_absent_tier_renders_as_unknown_never_as_zero(tmp_path):
    """`?` is a visible defect; `0` is a lie that reads as "this file is fine"."""
    prompt = tl.build_prompt(
        repo=tmp_path,
        sources=["m"],
        baseline_pytest_args="tests",
        candidate_pytest_args="tests",
        ranking=[{"path": "m.py"}],
    )
    assert "escaped-defect weight ?" in prompt
    assert "escaped-defect weight 0" not in prompt


def test_the_order_is_offered_not_mandated(tmp_path, on):
    """Selection is offered, never mandated — the prompt has to say so, or an agent will grind."""
    sources, ranking, _ = tl.ranked_sources(repo=_repo(tmp_path), limit=2)
    prompt = tl.build_prompt(
        repo=tmp_path,
        sources=sources,
        baseline_pytest_args="tests",
        candidate_pytest_args="tests",
        ranking=ranking,
    )
    assert "PRIORITY, not a mandate" in prompt
    assert "do not write a" in prompt and "smoke test to clear it" in prompt


# --------------------------------------------------------------------------------------------
# What it does when it cannot choose. Every one of these must FAIL TOWARD MOTION.
# --------------------------------------------------------------------------------------------


def test_the_switch_being_off_falls_back_to_hand_named_sources(tmp_path, off):
    sources, ranking, note = tl.ranked_sources(
        repo=_repo(tmp_path), limit=2, explicit_sources=["pkg"]
    )
    assert sources == ["pkg"]
    assert ranking == []
    assert tl.RANKING_SWITCH in note


def test_with_nothing_to_fall_back_on_the_note_is_a_diagnosis(tmp_path, off):
    sources, _, note = tl.ranked_sources(repo=_repo(tmp_path), limit=2)
    assert sources == []
    assert tl.RANKING_SWITCH in note and "none named" in note


def test_a_directory_that_is_not_a_repository_says_so(tmp_path, on):
    """The defect this whole path exists for.

    Both git-backed tiers go through a helper that returns "" on a non-zero exit, so a directory
    that is not a git repository produces exactly the empty ranking a pristine one does. Without
    the probe, a caller would read "no file needs tests" off a failed subprocess.
    """
    sources, _, note = tl.ranked_sources(repo=tmp_path, limit=2)
    assert sources == []
    assert "not a git repository" in note


def test_no_signal_and_unavailable_are_different_findings(tmp_path):
    """One sentinel must never mean both "measured zero" and "could not measure"."""
    rows, status = edp.rank_status(_repo(tmp_path / "real"), lookback_days=180)
    assert status["status"] == "ok" and rows

    empty = tmp_path / "empty"
    empty.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=empty, check=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=empty, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=empty, check=True)
    _, drained = edp.rank_status(empty)
    assert drained["status"] == "no_signal"
    assert "no coverage report" in drained["reason"], "it must say what it did NOT read"

    _, missing = edp.rank_status(tmp_path / "nowhere")
    assert missing["status"] == "unavailable"


def test_an_unreadable_coverage_report_is_named_in_the_note(tmp_path, on):
    _, _, note = tl.ranked_sources(
        repo=_repo(tmp_path), limit=1, coverage_json_path=str(tmp_path / "absent.json")
    )
    assert "UNREAD" in note and "absent.json" in note


def test_a_readable_coverage_report_feeds_tier_three(tmp_path, on):
    repo = _repo(tmp_path)
    report = tmp_path / "cov.json"
    report.write_text(
        json.dumps({"files": {"src/cold.py": {"summary": {"missing_lines": 40}}}}),
        encoding="utf-8",
    )
    _, ranking, note = tl.ranked_sources(repo=repo, limit=2, coverage_json_path=str(report))
    assert "tier 3 read from" in note
    by_path = {Path(r["path"]).name: r for r in ranking}
    assert by_path["cold.py"]["tier3_uncovered_statements"] == 40


def test_the_cli_exits_nonzero_rather_than_building_a_prompt_with_no_targets(tmp_path, on, capsys):
    """A prompt naming no sources would send an agent to write tests for nothing at all."""
    code = tl.main(
        [
            "--repo",
            str(tmp_path),
            "--rank-sources",
            "2",
            "--baseline-pytest-args",
            "tests",
            "--candidate-pytest-args",
            "tests",
        ]
    )
    assert code == 2
    assert "not a git repository" in capsys.readouterr().err


def test_hand_named_sources_still_work_with_no_ranking_at_all(tmp_path, off, capsys):
    """The wiring must not have made the old path conditional on the new one."""
    assert (
        tl.main(
            [
                "--repo",
                str(tmp_path),
                "--source",
                "pkg",
                "--baseline-pytest-args",
                "tests",
                "--candidate-pytest-args",
                "tests",
            ]
        )
        == 0
    )
    assert "--source pkg" in capsys.readouterr().out
