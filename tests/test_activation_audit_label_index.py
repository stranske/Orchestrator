"""The activation audit's fleet label index: a read that failed is UNREAD, never empty.

THE DEFECT (2026-10-04). `_fleet_label_index` served a 7-day cache. A stale cache was refreshed with
one `gh label list` per fleet repo; a repo whose read failed was skipped, and the result was then
written UNCONDITIONALLY as a fresh index. Reproduced against a fake `gh` before the fix:

* every read fails: `repos: {}` was trusted for 7 days, nothing was retried once GitHub answered
  again, and `vocabulary_mismatch` silently left the report;
* the four repos carrying `testing` fail: `label_absent_from_fleet` was asserted ("no repo carries a
  label producing 'testgen'") for a label that four of the twelve repos carry;
* an 8-day-old index of all twelve repos, refresh fails: all twelve were wiped.

Each moved the audit's graded `by_defect` projection, so an outage could mint "useful" verdicts in
both directions. These tests pin the relationship rather than the numbers: last good labels survive
a failed read, a failed repo is retried by the next audit run, a repo never read is unknown and
never a repo without the label, and the repos never read are declared in the finding population so the
tick's grader re-baselines that observation instead of grading the blindness.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

import capabilities
import capability_activation_audit as audit
import capability_propensity as propensity

CAP = "capability-activation-audit"
FLEET = ["o/alpha", "o/beta", "o/gamma", "o/delta"]
LABELS = {
    "o/alpha": ["bug", "testing"],
    "o/beta": ["bug", "risk:severe"],
    "o/gamma": ["bug", "testing"],
    "o/delta": ["bug"],
}
GOOD = {repo: sorted(labels) for repo, labels in LABELS.items()}
T0 = 1_800_000_000.0


class FakeGh:
    """Stands in for `gh label list`: records which repos were asked and never touches a network."""

    def __init__(self, *, fail=(), hang=()):
        self.fail, self.hang, self.asked = set(fail), set(hang), []

    def __call__(self, argv, **kwargs):
        assert argv[:3] == ["gh", "label", "list"], argv
        assert kwargs["timeout"] == audit.LABEL_READ_TIMEOUT_S, kwargs
        repo = argv[argv.index("--repo") + 1]
        self.asked.append(repo)
        if repo in self.hang:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        if repo in self.fail:
            return subprocess.CompletedProcess(argv, 1, "", "GraphQL: API rate limit exceeded")
        return subprocess.CompletedProcess(
            argv, 0, json.dumps([{"name": name} for name in LABELS[repo]]), ""
        )


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "STATE_DIR", tmp_path)
    monkeypatch.setattr(audit.backlog, "SUPPORTED_REPOS", list(FLEET))
    return tmp_path


def _index(gh, *, at: float, use_cache: bool = True) -> dict:
    return audit._fleet_label_index(use_cache=use_cache, runner=gh, now=T0 + at)


def _testgen_row(index: dict) -> dict:
    return audit.audit_capability(
        "testgen-probe",
        {"matcher": {"field": "task_type", "value": ["testgen"]}, "status": "generated"},
        emittable=audit.emittable_task_types(),
        templates=audit._prompt_templates(),
        label_index=index,
        vocab_gaps=audit.vocabulary_gaps(index),
    )


# --------------------------------------------------------------------------- the cache


def test_every_read_failing_caches_no_repo_as_read(state):
    idx = _index(FakeGh(fail=FLEET), at=0)
    assert idx["repos"] == {} and idx["unknown"] == FLEET, idx
    assert sorted(idx["unread"]) == sorted(FLEET), idx["unread"]
    assert all("rate limit" in v["reason"] for v in idx["unread"].values()), idx["unread"]
    cached = json.loads((state / audit.LABEL_INDEX_FILE).read_text())
    assert cached["repos"] == {} and sorted(cached["unread"]) == sorted(FLEET), cached


def test_a_failed_read_is_retried_by_the_next_daily_audit_not_a_week_later(state):
    # Measured against the constant below, so the bound on the constant itself is stated here: the
    # audit runs daily, and a window as long as that would leave a failed repo unread for a day.
    assert audit.LABEL_RETRY_S < 86400, "a failed repo would not be retried by the next daily run"
    _index(FakeGh(fail=FLEET), at=0)
    back = FakeGh()
    inside = _index(back, at=audit.LABEL_RETRY_S - 1)
    assert back.asked == [], "retried inside the retry window"
    assert inside["repos"] == {}, "a failed read was cached as a read of no labels"
    after = _index(back, at=audit.LABEL_RETRY_S)
    assert back.asked == FLEET, "a failed read was not retried after LABEL_RETRY_S"
    assert after["repos"] == GOOD and after["unread"] == {} and after["unknown"] == [], after


def test_only_the_repos_that_failed_are_read_again(state):
    _index(FakeGh(fail={"o/alpha", "o/gamma"}), at=0)
    back = FakeGh()
    _index(back, at=audit.LABEL_RETRY_S)
    # The two good reads are trusted for their own TTL; one repo's outage re-asks only that repo.
    assert back.asked == ["o/alpha", "o/gamma"], back.asked


def test_a_stale_good_index_survives_the_refresh_that_fails(state):
    _index(FakeGh(), at=0)
    gh = FakeGh(fail=FLEET)
    stale = _index(gh, at=audit.LABEL_READ_TTL_S)
    assert gh.asked == FLEET, "a stale index was not refreshed"
    assert stale["repos"] == GOOD, "a failed refresh wiped the last good labels"
    assert stale["unknown"] == [] and sorted(stale["unread"]) == sorted(FLEET), stale
    # Served with the age of what is served, so a reader can see how old the evidence is.
    summary = audit.label_index_summary(stale)
    assert summary["read"] == 0 and summary["unknown"] == [], summary
    assert summary["unread"]["o/alpha"]["last_good_read_at"] == audit._iso(T0), summary


def test_a_hung_network_costs_the_breaker_not_one_timeout_per_repo(state):
    gh = FakeGh(hang=FLEET)
    idx = _index(gh, at=0)
    assert gh.asked == FLEET[: audit.LABEL_TIMEOUT_BREAKER], gh.asked
    assert sorted(idx["unread"]) == sorted(FLEET), idx["unread"]
    untried = {r: v["reason"] for r, v in idx["unread"].items() if r not in gh.asked}
    assert untried and all(why.startswith("not tried") for why in untried.values()), untried
    # A read that answers resets the count, so hangs between good reads stop nothing.
    gaps = FakeGh(hang={"o/alpha", "o/gamma"})
    again = _index(gaps, at=60, use_cache=False)
    assert gaps.asked == FLEET, gaps.asked
    assert sorted(again["unread"]) == ["o/alpha", "o/gamma"], again["unread"]


def test_no_gh_binary_is_every_repo_unread_and_runs_nothing(state, monkeypatch):
    def no_subprocess(*args, **kwargs):
        raise AssertionError("ran a subprocess with no gh installed")

    monkeypatch.setattr(audit.shutil, "which", lambda name: None)
    monkeypatch.setattr(audit.subprocess, "run", no_subprocess)
    idx = audit._fleet_label_index(now=T0)
    assert idx["repos"] == {} and idx["unknown"] == FLEET, idx
    assert {v["reason"] for v in idx["unread"].values()} == {"gh CLI not installed"}, idx


def test_no_cache_rereads_every_repo_and_still_keeps_last_good_labels(state):
    _index(FakeGh(), at=0)
    gh = FakeGh(fail={"o/beta"})
    idx = _index(gh, at=60, use_cache=False)
    assert gh.asked == FLEET, gh.asked
    assert idx["repos"] == GOOD and list(idx["unread"]) == ["o/beta"], idx


@pytest.mark.parametrize(
    "held, asked",
    [
        (FLEET[:3], ["o/delta"]),  # partial: only the repo it lacks is read
        ([], FLEET),  # EMPTY, as an outage wrote it: every repo is read at once
    ],
    ids=["partial", "empty"],
)
def test_a_cache_from_before_per_repo_stamps_reads_as_one_read(state, held, asked):
    old = {"generated_at": T0, "repos": {repo: GOOD[repo] for repo in held}}
    (state / audit.LABEL_INDEX_FILE).write_text(json.dumps(old))
    gh = FakeGh()
    idx = _index(gh, at=3600)
    assert gh.asked == asked, gh.asked
    assert idx["repos"] == GOOD and idx["unknown"] == [], idx


@pytest.mark.parametrize("stdout", ["", "not json", '{"message": "Not Found"}', '[{"nom": 1}]'])
def test_output_gh_could_not_have_meant_is_unread_never_empty(stdout):
    labels, why, timed_out = audit._read_repo_labels(
        "o/x", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout, "")
    )
    assert labels is None and why.startswith("unparseable gh output") and not timed_out, why


def test_a_repo_with_no_labels_is_read_as_empty_not_unread():
    labels, why, timed_out = audit._read_repo_labels(
        "o/x", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "[]", "")
    )
    assert (labels, why, timed_out) == ([], "", False)


def test_the_real_gh_path_reads_through_a_fake_gh_on_path(state, monkeypatch):
    """No runner: `shutil.which` finds `gh` and `subprocess.run` calls it with the real argv."""
    bin_dir = state / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'case "$*" in *o/beta*) echo "HTTP 502: Bad Gateway" >&2; exit 1;; esac\n'
        'echo \'[{"name": "Bug"}, {"name": " Testing "}]\'\n'
    )
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    idx = audit._fleet_label_index(now=T0)
    assert idx["repos"]["o/alpha"] == ["bug", "testing"], idx["repos"]
    assert "HTTP 502" in idx["unread"]["o/beta"]["reason"], idx["unread"]
    assert idx["unknown"] == ["o/beta"], idx


# --------------------------------------------------------------------------- the findings


def test_a_repo_never_read_is_not_a_repo_without_the_label(state):
    # Both carriers of `testing` fail. Before the fix this asserted that no repo carries it.
    idx = _index(FakeGh(fail={"o/alpha", "o/gamma"}), at=0)
    cov = audit.label_coverage("testgen", idx)
    assert (cov["repos_with"], cov["repos_total"]) == (0, 4), cov
    assert cov["repos_unknown"] == ["alpha", "gamma"], cov
    assert cov["missing_in"] == ["beta", "delta"], cov
    row = _testgen_row(idx)
    assert "label_absent_from_fleet" not in row["defects"], row
    assert any("unknown" in note and "never read" in note for note in row["notes"]), row["notes"]


def _old_rule(have: int, total: int) -> bool:
    """`label_absent_from_fleet` as the audit decided it before any repo could be unknown."""
    return have == 0 or have <= max(1, (total or 12) // 6)


def test_with_every_repo_read_the_finding_is_exactly_the_old_rule():
    """So a deploy over a complete label cache cannot move `by_defect` through this rule."""
    for total in range(1, 25):
        for have in range(total + 1):
            cov = {"labels": ["testing"], "repos_with": have, "repos_total": total}
            short, why = audit.label_shortfall("testgen", {**cov, "repos_unknown": []})
            assert short is _old_rule(have, total), (have, total, short)
            assert bool(why) is short, (have, total, why)
    assert audit.label_shortfall(
        "testgen", {"labels": ["testing"], "repos_with": 0, "repos_total": 12}
    ) == (True, "no repo carries a label producing 'testgen'")
    assert audit.label_shortfall(
        "testgen", {"labels": ["testing"], "repos_with": 2, "repos_total": 12}
    ) == (True, "a label producing 'testgen' exists in only 2/12 repos")


def test_with_repos_never_read_a_finding_is_asserted_or_ruled_out_only_when_certain():
    for total in range(2, 19):
        for unknown in range(1, total):
            for have in range(total - unknown + 1):
                # Every way the repos never read could turn out, judged by the old rule.
                outcomes = {_old_rule(have + extra, total) for extra in range(unknown + 1)}
                cov = {
                    "labels": ["testing"],
                    "repos_with": have,
                    "repos_total": total,
                    "repos_unknown": ["never-read"] * unknown,
                }
                short, why = audit.label_shortfall("testgen", cov)
                expected = next(iter(outcomes)) if len(outcomes) == 1 else None
                assert short is expected, (have, unknown, total, short, outcomes)
                if short is None:
                    assert "unknown" in why and "never read" in why, why


def test_nothing_read_at_all_is_unknown_with_a_note():
    cov = audit.label_coverage("testgen", {"repos": {}, "unknown": FLEET})
    assert cov["repos_with"] is None and cov["repos_total"] == 4, cov
    short, why = audit.label_shortfall("testgen", cov)
    assert short is None and "no repo's labels were read" in why, why


# --------------------------------------------------------------------------- the grader


def _drift(prior_population, population) -> str | None:
    """What `tick_evidence` asks before grading: is this observation comparable with the last?"""
    prior = {
        "findings": {"by_defect": []},
        "population": prior_population,
        "projection": propensity._projection_signature(CAP),
    }
    return propensity._projection_drift(CAP, prior, {"by_defect": []}, population)


def test_a_blind_observation_is_rebaselined_and_a_stale_one_is_graded(state, monkeypatch):
    complete = audit.finding_population(_index(FakeGh(), at=0))
    stale = audit.finding_population(_index(FakeGh(fail=FLEET), at=audit.LABEL_READ_TTL_S))
    monkeypatch.setattr(audit, "STATE_DIR", state / "fresh")
    blind = audit.finding_population(_index(FakeGh(fail={"o/beta"}), at=0))
    assert blind["label_repos_unknown"] == ["o/beta"], blind
    # Last good labels are what the last good read produced, so that observation is graded...
    assert stale == complete and _drift(complete, stale) is None, (complete, stale)
    # ...while a repo never read can neither assert nor rule out a finding: no verdict, either way.
    assert "population" in (_drift(complete, blind) or ""), blind
    assert "population" in (_drift(blind, complete) or ""), blind


def test_the_edit_itself_rebaselines_the_grader():
    """Reports from before this change declared the live-row rule alone, so the first one after it
    is a first observation, and a partial cache written before it cannot mint a verdict."""
    before = capabilities.live_finding_population()
    after = audit.finding_population({"repos": {}})
    assert after["label_evidence"] == audit.LABEL_EVIDENCE, after
    assert "population" in (_drift(before, after) or ""), after


# --------------------------------------------------------------------------- the report


def test_fully_read_renders_as_read_and_the_unread_are_named_beside_the_count(state, monkeypatch):
    complete = audit.label_index_summary(_index(FakeGh(), at=0))
    assert audit.format_label_line(complete) == "  fleet labels: 4/4 repos read", complete
    stale = audit.label_index_summary(_index(FakeGh(fail={"o/beta"}), at=audit.LABEL_READ_TTL_S))
    line = audit.format_label_line(stale)
    assert "3/4 repos read; UNREAD 1 (beta); serving their last good labels" in line, line
    assert audit._iso(T0 + audit.LABEL_READ_TTL_S + audit.LABEL_RETRY_S) in line, line
    monkeypatch.setattr(audit, "STATE_DIR", state / "fresh")
    blind = audit.label_index_summary(_index(FakeGh(fail={"o/beta"}), at=0))
    assert "1 never read, so their labels are UNKNOWN, not empty" in audit.format_label_line(blind)
    assert audit.format_label_line(None) == "  fleet labels: not in this report"


def test_the_report_carries_the_label_evidence_beside_its_findings(tmp_path, monkeypatch):
    ledger = tmp_path / "capabilities.json"
    row = capabilities._blank_capability("t-routed")
    row.update(
        status="wired", matcher={"field": "task_type", "operator": "in", "value": ["testgen"]}
    )
    capabilities.save({"t-routed": row}, ledger)
    index = {
        "repos": {"o/alpha": ["testing"]},
        "read_at": {"o/alpha": T0},
        "unread": {"o/beta": {"reason": "gh exit 1: x", "failed_at": T0}},
        "unknown": ["o/beta"],
        "fleet": ["o/alpha", "o/beta"],
    }
    tree = tmp_path / "tree"
    tree.mkdir()
    monkeypatch.setattr(audit, "HERE", tree)
    monkeypatch.setattr(audit, "sibling_checkouts", lambda: [])
    monkeypatch.setattr(audit, "_fleet_label_index", lambda use_cache=True: index)
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    rep = audit.audit(path=ledger, use_cache=True)
    assert rep["fleet_labels"]["unknown"] == ["o/beta"], rep["fleet_labels"]
    assert rep[capabilities.FINDING_POPULATION_KEY]["label_repos_unknown"] == ["o/beta"], rep
    assert "label_absent_from_fleet" not in rep["by_defect"], rep["by_defect"]
    assert "fleet labels: 1/2 repos read; UNREAD 1 (beta)" in audit.format_scorecard(rep)
