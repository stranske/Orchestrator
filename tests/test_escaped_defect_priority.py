"""The ranking's invariants, pinned where the hollow detector can grade them.

These are pytest tests rather than selftest cases on purpose, and the reason is the subject of the
module they test: `local_verify` grades per pytest NODE, so a selftest — one exit code — is a
single node and its internal assertions are invisible to hollow-test detection. A module whose job
is to order test-writing work should have its own tests gradeable by the gate that judges the work
it orders.

The module keeps a `--selftest` too. The two are complementary: the selftest exercises the CLI the
way it ships, including live `git log` parsing against a real temporary repository; these pin the
ordering rules as pure functions, where a break is attributable to one invariant.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import escaped_defect_priority as edp


def _repo(tmp_path: Path, commits: list[tuple[str, dict[str, str]]]) -> Path:
    """Build a real git repo. Real, not mocked: the parsing is the part that breaks."""
    subprocess.run(["git", "init", "-q", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    for subject, files in commits:
        for name, body in files.items():
            target = tmp_path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-qm", subject], cwd=tmp_path, check=True)
    return tmp_path


# ---------------------------------------------------------------------------------------------
# Tier ordering. The whole point of the module is that the tiers do not trade off against each
# other on volume, so each of these asserts a STRICT ordering rather than a score value.
# ---------------------------------------------------------------------------------------------


def test_one_escaped_defect_outranks_any_amount_of_uncovered_code():
    """This is the design decision, stated as an assertion.

    Ranking by uncovered mass points agents at the largest files, which are the glue modules where
    hollow tests get written. A file that actually needed a bug fix is evidence about the TESTS;
    an uncovered count is only evidence about the code.
    """
    defect = edp.FileScore(path="a.py", escaped=1.0)
    # 10**9, not a modest number: the first implementation multiplied the tiers apart and summed
    # them, which inverts once tier 3 reaches the multiplier. The tuple holds at any magnitude.
    bulk = edp.FileScore(path="b.py", uncovered=10**9)
    assert defect.sort_key() > bulk.sort_key()


def test_churn_outranks_uncovered_but_loses_to_a_defect():
    defect = edp.FileScore(path="a.py", escaped=1.0)
    churny = edp.FileScore(path="b.py", churn=10**6)
    bulk = edp.FileScore(path="c.py", uncovered=10**9)
    assert defect.sort_key() > churny.sort_key() > bulk.sort_key()


def test_a_revert_counts_for_more_than_an_ordinary_fix():
    """Somebody judging a change wrong outright is stronger evidence than a follow-up fix."""
    assert edp.REVERT_WEIGHT > edp.FIX_WEIGHT


# ---------------------------------------------------------------------------------------------
# The hollow multiplier — the term that keeps this from becoming another metric to game.
# ---------------------------------------------------------------------------------------------


def test_a_fully_hollow_history_sinks_a_file_to_zero():
    """A module where every generated test passes against a broken base is worth no more work.

    Without this term the ranking would keep sending agents at the same untestable glue forever,
    and each visit would raise coverage while proving nothing.
    """
    hollow = edp.FileScore(path="a.py", escaped=99.0, churn=99, uncovered=99, hollow_rate=1.0)
    assert hollow.sort_key() == (0.0, 0.0, 0.0)


def test_hollow_rate_scales_rather_than_switches():
    full = edp.FileScore(path="a.py", escaped=1.0)
    half = edp.FileScore(path="a.py", escaped=1.0, hollow_rate=0.5)
    assert half.sort_key()[0] == full.sort_key()[0] / 2


def test_an_unmeasured_file_is_not_treated_as_a_good_one(tmp_path):
    """No hollow history means no penalty — and that is deliberate, not an oversight.

    Unmeasured is not the same as measured-good, but the first attempt on a file is what produces
    the measurement, so a file nobody has tried must stay reachable. The penalty applies once
    there is evidence, never before.
    """
    repo = _repo(tmp_path, [("fix: boom", {"m.py": "x = 1\n"})])
    ranked = edp.rank(repo, {})
    assert ranked[0]["path"] == "m.py"
    assert ranked[0]["hollow_rate"] == 0.0


# ---------------------------------------------------------------------------------------------
# Could-not-measure is never measured-zero. The module's own first draft got this wrong: a
# missing coverage report fell through to an empty dict and tier 3 rendered as a column of zeros,
# which reads as "everything is covered" rather than "nothing was read".
# ---------------------------------------------------------------------------------------------


def test_absolute_paths_are_dropped_as_contamination():
    """A tmp-workspace copy is not a file anyone can open, so it must not be ranked.

    That contamination put 95 phantom rows into a real Gate payload and filled 13 of its 15
    worst-file slots, which is how a coverage report came to point at files that do not exist.
    """
    cov = {
        "files": {
            "src/real.py": {"summary": {"missing_lines": 10}},
            "/tmp/pytest-of-runner/ws/src/real.py": {"summary": {"missing_lines": 900}},
        }
    }
    assert edp.uncovered_by_file(cov) == {"src/real.py": 10}


def test_a_missing_coverage_report_is_reported_not_silently_empty(tmp_path, capsys):
    repo = _repo(tmp_path, [("fix: boom", {"m.py": "x = 1\n"})])
    edp.main(["--repo", str(repo), "--coverage-json", str(tmp_path / "absent.json")])
    out = capsys.readouterr().out
    assert "UNAVAILABLE" in out
    assert "does not exist" in out


def test_an_unparseable_coverage_report_names_the_failure(tmp_path, capsys):
    repo = _repo(tmp_path, [("fix: boom", {"m.py": "x = 1\n"})])
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    edp.main(["--repo", str(repo), "--coverage-json", str(bad)])
    out = capsys.readouterr().out
    assert "UNAVAILABLE" in out and "unreadable" in out


def test_a_brain_that_cannot_be_read_reports_unknown_not_zero():
    """`unknown` and `no escaped defects` are opposite findings; only one is good news."""
    status = edp.brain_signal_status("/no/such/store.db")
    assert status["brain"] == "unknown"
    assert status["source"] == "git"


# ---------------------------------------------------------------------------------------------
# Commit-subject matching, against the forms real history actually uses.
# ---------------------------------------------------------------------------------------------


def test_fix_subjects_match_and_prose_about_fixing_does_not():
    assert edp.FIX_SUBJECT.search("fix(coverage): omit tmp rows")
    assert edp.FIX_SUBJECT.search("fix: null deref")
    assert edp.FIX_SUBJECT.search("Fixed the crash")
    # The anchor is what stops a feature commit that merely mentions fixing from scoring.
    assert not edp.FIX_SUBJECT.search("feat: prefix the fix with a scope")
    assert not edp.FIX_SUBJECT.search("docs: explain how to fix it")


def test_ranking_ignores_non_python_and_test_files(tmp_path):
    """Churn on a lockfile is true and useless; a test file is not a testgen target."""
    repo = _repo(
        tmp_path,
        [
            (
                "fix: touch several things",
                {
                    "m.py": "x = 1\n",
                    "requirements.lock": "pkg==1\n",
                    "tests/test_m.py": "def test_x():\n    assert True\n",
                },
            )
        ],
    )
    paths = [r["path"] for r in edp.rank(repo, {})]
    assert "m.py" in paths
    assert "requirements.lock" not in paths
    assert "tests/test_m.py" not in paths


def test_json_output_carries_both_source_declarations(tmp_path, capsys):
    """A consumer must be able to tell which tier-1 source produced the order it is acting on."""
    repo = _repo(tmp_path, [("fix: boom", {"m.py": "x = 1\n"})])
    edp.main(["--repo", str(repo), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert "tier1_source" in payload and "tier3_coverage" in payload
    assert payload["tier1_source"]["source"] in {"git", "brain"}
