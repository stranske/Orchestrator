"""The coverage policy: what a repository's number buys, and what it must never buy.

Pytest rather than selftest cases so `local_verify` can grade them per node, and because the
policy is the kind of thing a future change will edit by eye — the thresholds are two numbers in
a file and the difference between them is an argument, not a constant.
"""

from __future__ import annotations

import json

import pytest

import coverage_testgen_trigger as trigger

# ---------------------------------------------------------------------------------------------
# The machine threshold is a LEVEL. Every boundary asserted, because 90 vs 89.99 is exactly the
# kind of edge a refactor moves by one comparison operator.
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "coverage, action",
    [
        (100.0, trigger.ACTION_NONE),
        (90.01, trigger.ACTION_NONE),
        (90.0, trigger.ACTION_NONE),
        (89.99, trigger.ACTION_WRITE),
        (85.0, trigger.ACTION_WRITE),
        (0.0, trigger.ACTION_WRITE),
    ],
)
def test_the_write_threshold_is_at_the_target_not_below_it(coverage, action):
    """90.0 exactly is AT the target. A repo that just reached it must not be sent back to work."""
    assert trigger.decide("r", coverage).action == action


# ---------------------------------------------------------------------------------------------
# The human threshold is a CROSSING, and that is the whole attention-cost argument.
# ---------------------------------------------------------------------------------------------


def test_a_repo_that_falls_below_the_warning_line_is_reported_once():
    d = trigger.decide("r", 84.0, previous=86.0)
    assert d.warn_human is True
    assert "CROSSED" in d.reason


def test_a_repo_already_below_the_line_is_not_reported_again():
    """The arithmetic this design turns on.

    Four of twelve in-scope repos are below 85 today. Warning on every cycle while a repo sits
    there is 4 x 52 = 208 notices a year for four facts already known — roughly 1.7 hours of
    reading whose entire content is "still below the line". That does not overflow a budget so
    much as teach its reader to ignore the channel, and an ignored channel is worse than none.
    """
    assert trigger.decide("r", 84.0, previous=80.0).warn_human is False
    assert trigger.decide("r", 84.0, previous=84.0).warn_human is False


def test_a_first_reading_is_not_treated_as_a_regression():
    """With no previous figure there is no fall to report — only a state, and the machine has it."""
    d = trigger.decide("r", 70.0, previous=None)
    assert d.warn_human is False
    assert d.action == trigger.ACTION_WRITE


def test_the_machine_keeps_working_whether_or_not_the_human_is_told():
    """The two thresholds are independent. Silence must never mean the work stopped."""
    for previous in (None, 80.0, 86.0):
        assert trigger.decide("r", 84.0, previous=previous).action == trigger.ACTION_WRITE


def test_between_the_thresholds_nobody_is_disturbed():
    d = trigger.decide("r", 87.0, previous=95.0)
    assert d.action == trigger.ACTION_WRITE
    assert d.warn_human is False, "the warning line is 85, not 'any drop'"


# ---------------------------------------------------------------------------------------------
# Unknown is never zero. The defect this whole programme has been unwinding, at the last step.
# ---------------------------------------------------------------------------------------------


def test_unreadable_coverage_buys_nothing_at_all():
    """Treating an unreadable figure as 0 would buy the MOST test-writing exactly where the
    measurement is broken — work that cannot be verified, ordered by a number nobody produced."""
    d = trigger.decide("r", None)
    assert d.action == trigger.ACTION_UNKNOWN
    assert d.warn_human is False
    assert d.action != trigger.ACTION_WRITE


def test_zero_and_unknown_are_different_decisions():
    assert trigger.decide("r", 0.0).action == trigger.ACTION_WRITE
    assert trigger.decide("r", None).action == trigger.ACTION_UNKNOWN


def test_an_unknown_reading_never_warns_a_human():
    """A broken instrument is not a regression, and reporting it as one spends attention on the
    wrong problem."""
    assert trigger.decide("r", None, previous=95.0).warn_human is False


# ---------------------------------------------------------------------------------------------
# Reading the report. Every failure names itself.
# ---------------------------------------------------------------------------------------------


def test_a_real_total_is_read(tmp_path):
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps({"totals": {"percent_covered": 76.72}}), encoding="utf-8")
    assert trigger.coverage_from_report(report) == (76.72, "ok")


@pytest.mark.parametrize(
    "content, needle",
    [
        (None, "no coverage report"),
        ("{not json", "unreadable"),
        (json.dumps({"files": {}}), "no totals.percent_covered"),
        (json.dumps({"totals": {"percent_covered": "not a number"}}), "non-numeric"),
    ],
)
def test_every_unreadable_shape_is_named(tmp_path, content, needle):
    report = tmp_path / "coverage.json"
    if content is not None:
        report.write_text(content, encoding="utf-8")
    percent, status = trigger.coverage_from_report(report)
    assert percent is None
    assert needle in status, status


# ---------------------------------------------------------------------------------------------
# The kill switch, and the CLI contract.
# ---------------------------------------------------------------------------------------------


def test_the_kill_switch_decides_nothing_and_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(trigger.KILL_SWITCH, raising=False)
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps({"totals": {"percent_covered": 10.0}}), encoding="utf-8")
    assert trigger.main(["--coverage-json", str(report), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert trigger.KILL_SWITCH in payload["skipped"]
    assert "action" not in payload, "a disabled trigger must not emit a decision at all"


def test_with_the_switch_on_a_decision_is_emitted(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(trigger.KILL_SWITCH, "1")
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps({"totals": {"percent_covered": 76.72}}), encoding="utf-8")
    assert trigger.main(["--repo", "r", "--coverage-json", str(report), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == trigger.ACTION_WRITE
    assert payload["coverage"] == 76.72


def test_the_reading_failure_reaches_the_decision(tmp_path, monkeypatch, capsys):
    """ "Unknown" alone strands a reader; WHICH report and WHY is what makes it actionable."""
    monkeypatch.setenv(trigger.KILL_SWITCH, "1")
    assert trigger.main(["--coverage-json", str(tmp_path / "absent.json"), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == trigger.ACTION_UNKNOWN
    assert "no coverage report" in payload["reason"]


# ---------------------------------------------------------------------------------------------
# Naming WHERE. A decision that says "write tests" and names nowhere is not actionable, and an
# empty target list must never be the rendering of three different findings.
# ---------------------------------------------------------------------------------------------


def _repo_with_history(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "hot.py").write_text("x = 1\n", encoding="utf-8")
    for cmd in (
        ["git", "init", "-q", "."],
        ["git", "config", "user.email", "a@b.c"],
        ["git", "config", "user.name", "t"],
        ["git", "add", "-A"],
        ["git", "commit", "-qm", "fix: a bug slipped through"],
    ):
        subprocess.run(cmd, cwd=repo, check=True)
    return repo


def test_targets_are_named_when_a_checkout_is_given(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_ESCAPED_DEFECT_PRIORITY", "1")
    repo = _repo_with_history(tmp_path)
    cov = {"files": {"hot.py": {"summary": {"missing_lines": 4}}}}
    targets, note = trigger.rank_targets(repo, cov, 3)
    assert targets == ["hot.py"]
    assert "ranked 1" in note


def test_the_ranker_being_switched_off_is_said_not_shown_as_no_targets(tmp_path, monkeypatch):
    """Three findings, one empty list — which is why the note carries the difference.

    "Nothing to rank", "the ranker is off" and "I was given no checkout" all produce `targets:
    []`. Left there, the most actionable decision the trigger can make would render identically
    to the least.
    """
    monkeypatch.delenv("ORCH_ESCAPED_DEFECT_PRIORITY", raising=False)
    repo = _repo_with_history(tmp_path)
    targets, note = trigger.rank_targets(
        repo, {"files": {"hot.py": {"summary": {"missing_lines": 4}}}}, 3
    )
    assert targets == []
    assert "could not rank" in note
    assert "ORCH_ESCAPED_DEFECT_PRIORITY" in note


def test_an_unrankable_repo_says_why(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_ESCAPED_DEFECT_PRIORITY", "1")
    targets, note = trigger.rank_targets(tmp_path / "not-a-repo", {}, 3)
    assert targets == []
    assert "could not rank" in note


def test_ranked_and_found_nothing_is_distinct_from_could_not_rank(tmp_path, monkeypatch):
    """The good-news zero and the no-news zero, kept apart at the last step of the chain."""
    monkeypatch.setenv("ORCH_ESCAPED_DEFECT_PRIORITY", "1")
    repo = _repo_with_history(tmp_path)
    # A report that measures a file the repo does not have: ranking runs, nothing qualifies.
    targets, note = trigger.rank_targets(
        repo, {"files": {"elsewhere.py": {"summary": {"missing_lines": 9}}}}, 3
    )
    assert targets == []
    assert "could not rank" not in note


def test_without_a_checkout_the_decision_says_nothing_ranked_them(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(trigger.KILL_SWITCH, "1")
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps({"totals": {"percent_covered": 50.0}}), encoding="utf-8")
    trigger.main(["--repo", "r", "--coverage-json", str(report), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["targets"] == []
    assert "--repo-path was not given" in payload["reason"]


def test_a_repo_at_target_names_no_targets_and_does_not_pretend_to_rank(
    tmp_path, monkeypatch, capsys
):
    """Above 90 there is no work, so ranking would be effort spent to produce an unused list."""
    monkeypatch.setenv(trigger.KILL_SWITCH, "1")
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps({"totals": {"percent_covered": 95.0}}), encoding="utf-8")
    trigger.main(
        ["--repo", "r", "--repo-path", str(tmp_path), "--coverage-json", str(report), "--json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == trigger.ACTION_NONE
    assert payload["targets"] == []
    assert "rank" not in payload["reason"].lower()
