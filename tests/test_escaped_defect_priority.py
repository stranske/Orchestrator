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

import contextlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import capabilities
import escaped_defect_priority as edp


@pytest.fixture(autouse=True)
def isolated_priority_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state = tmp_path / "_state"
    runtime = tmp_path / "_runtime"
    state.mkdir()
    runtime.mkdir()
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(runtime))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(capabilities, "production_heartbeat", lambda *a, **k: None)

    loaded_fb = sys.modules.get("feedback")
    if loaded_fb is not None:
        monkeypatch.setattr(loaded_fb, "DB_PATH", runtime / "feedback.db")

    real_popen = subprocess.Popen
    attempted: list[tuple] = []

    def guarded_popen(cmd, *args, **kwargs):
        argv = list(cmd) if isinstance(cmd, (list, tuple)) else [str(cmd)]
        if not argv or os.path.basename(str(argv[0])) != "git":
            attempted.append((argv, kwargs))
            raise AssertionError(f"only local git subprocesses are permitted: {argv}")
        if any(tok in {"fetch", "push", "pull", "clone", "ls-remote"} for tok in argv[1:]):
            attempted.append((argv, kwargs))
            raise AssertionError(f"network git subprocess forbidden: {argv}")
        return real_popen(cmd, *args, **kwargs)

    def forbidden(*args, **kwargs):
        attempted.append((args, kwargs))
        raise AssertionError("escaped_defect_priority tests must not open sockets")

    monkeypatch.setattr(subprocess, "Popen", guarded_popen)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    yield
    assert attempted == [], f"unexpected external calls: {attempted}"


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


# ---------------------------------------------------------------------------------------------
# A reorganised repository. These were written after the ranking was used for real on this repo
# and returned two rows for one module plus a third for a path that no longer exists.
# ---------------------------------------------------------------------------------------------


def _moved_repo(tmp_path: Path) -> Path:
    """History that names a module by two paths, exactly as the `src/` move left this repo."""
    repo = _repo(tmp_path, [("fix: first bug", {"m.py": "x = 1\n"})])
    subprocess.run(["git", "mv", "m.py", "src_m.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "refactor: move it"], cwd=repo, check=True)
    (repo / "src_m.py").write_text("x = 2\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-aqm", "fix: second bug"], cwd=repo, check=True)
    return repo


def test_a_module_that_moved_is_one_candidate_holding_all_its_evidence(tmp_path):
    """Two rows at half strength each is the wrong answer twice over.

    It splits one module's evidence, and it spends a queue slot on a path an agent cannot open.
    Measured on this repository the first time the ranking was used for real: `capability_advisor`
    appeared under both its old and new names, and `dispatcher.py` outranked `src/verify.py` while
    not existing at that path at all.
    """
    ranked = edp.rank(_moved_repo(tmp_path), {})
    paths = [r["path"] for r in ranked]
    assert paths == ["src_m.py"], paths
    # Both fixes land on the one row: the pre-move fix is not lost with the old name.
    assert ranked[0]["tier1_escaped_defects"] == 2.0


def test_rename_chains_are_followed_to_the_end(tmp_path):
    """A file moved twice must not resolve to the intermediate name, which is equally gone."""
    repo = _repo(tmp_path, [("fix: bug", {"a.py": "x = 1\n"})])
    subprocess.run(["git", "mv", "a.py", "b.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "refactor: a to b"], cwd=repo, check=True)
    subprocess.run(["git", "mv", "b.py", "c.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "refactor: b to c"], cwd=repo, check=True)
    assert edp.rename_map(repo)["a.py"] == "c.py"
    assert [r["path"] for r in edp.rank(repo, {})] == ["c.py"]


def test_a_deleted_file_is_not_offered_as_a_testgen_target(tmp_path):
    repo = _repo(tmp_path, [("fix: bug", {"gone.py": "x = 1\n", "stays.py": "y = 1\n"})])
    subprocess.run(["git", "rm", "-q", "gone.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "chore: drop it"], cwd=repo, check=True)
    assert [r["path"] for r in edp.rank(repo, {})] == ["stays.py"]


def test_a_window_that_predates_a_move_says_so_rather_than_reporting_no_work(tmp_path):
    """The empty result and its cause, kept apart.

    If every scored path has since vanished, the honest finding is that the history and the
    checkout disagree — not that no file needs tests.
    """
    repo = _repo(tmp_path, [("fix: bug", {"gone.py": "x = 1\n"})])
    subprocess.run(["git", "rm", "-q", "gone.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "chore: drop it"], cwd=repo, check=True)
    rows, status = edp.rank_status(repo)
    assert rows == []
    assert status["status"] == "no_signal"
    assert "NONE still exists" in status["reason"], status["reason"]


# ---------------------------------------------------------------------------------------------
# Measurability. A candidate the supplied report cannot measure is not a candidate: no test
# written for it can be shown to have worked, because nothing observes the file being edited.
# ---------------------------------------------------------------------------------------------


def test_a_file_the_report_does_not_measure_is_excluded_and_named(tmp_path):
    """The real case: stranske/Workflows' templates/consumer-repo/ mirrors the repo root.

    Those copies carry genuine fix commits and churn — sync commits touch them — and sit outside
    every coverage scope, so they scored 0 uncovered and took HALF the top ten. An agent sent
    there cannot raise coverage whatever it writes.
    """
    repo = _repo(
        tmp_path,
        [("fix: touch both copies", {"real.py": "x = 1\n", "templates/copy.py": "x = 1\n"})],
    )
    cov = {"files": {"real.py": {"summary": {"missing_lines": 3}}}}
    rows, status = edp.rank_status(repo, cov)
    paths = [r["path"] for r in rows]
    assert paths == ["real.py"], paths
    assert "templates/copy.py" in status["reason"]
    assert "EXCLUDED as unmeasurable" in status["reason"]


def test_a_fully_covered_file_is_still_a_candidate(tmp_path):
    """The distinction the whole change rests on.

    A file present in the report with nothing missing is MEASURED and fully covered; a file absent
    is not measured at all. Both used to render as `uncovered = 0`. Only the second is disqualified
    — treating the first as unmeasurable would drop a high-churn file that a new test could still
    protect, and the escaped-defect tier is exactly about files whose EXISTING tests missed
    something.
    """
    repo = _repo(tmp_path, [("fix: a bug slipped through", {"covered.py": "x = 1\n"})])
    cov = {"files": {"covered.py": {"summary": {"missing_lines": 0}}}}
    rows, status = edp.rank_status(repo, cov)
    assert [r["path"] for r in rows] == ["covered.py"]
    assert "EXCLUDED" not in status["reason"]


def test_with_no_report_supplied_nothing_is_excluded_for_being_unmeasured(tmp_path):
    """Tier 3 dark for everyone carries no information, so excluding on it would rank nothing."""
    repo = _repo(tmp_path, [("fix: bug", {"a.py": "x = 1\n", "b.py": "y = 1\n"})])
    rows, status = edp.rank_status(repo, {})
    assert {r["path"] for r in rows} == {"a.py", "b.py"}
    assert "EXCLUDED" not in status["reason"]


def test_a_report_of_only_tmp_paths_measures_nothing_and_says_so(tmp_path):
    """A contaminated report is not a measurement, and must not read as "no work to do".

    NOTE ON WHAT THIS DOES AND DOES NOT PIN. It was written as
    `test_absolute_paths_..._do_not_make_real_files_measurable`, and a break demo showed it passes
    whether or not `measured_files` filters absolute paths — candidates come from git as RELATIVE
    paths, so an absolute entry can never match one either way. The filter there is consistency
    with `uncovered_by_file`, not a guard, and a test implying otherwise is the kind that looks
    like protection and is not.

    What IS worth pinning is the outcome: a report naming only tmp-workspace copies measures no
    real file, so every candidate is excluded AND NAMED, which distinguishes a contaminated report
    from a repository with nothing left to test.
    """
    repo = _repo(tmp_path, [("fix: bug", {"real.py": "x = 1\n"})])
    cov = {"files": {"/tmp/ws/real.py": {"summary": {"missing_lines": 5}}}}
    rows, status = edp.rank_status(repo, cov)
    assert rows == []
    assert "real.py" in status["reason"]
    assert "EXCLUDED as unmeasurable" in status["reason"], (
        "an empty ranking from a contaminated report must say why; 'no Python source scored' is "
        "the good-news reading of 'nothing here could be measured'"
    )


def test_both_test_file_conventions_are_excluded(tmp_path):
    """`test_*.py` and `*_test.py` are both test files, and neither is a testgen target."""
    repo = _repo(
        tmp_path,
        [
            (
                "fix: bug",
                {
                    "mod.py": "x = 1\n",
                    "test_mod.py": "def test_a():\n    assert True\n",
                    "mod_test.py": "def test_b():\n    assert True\n",
                },
            )
        ],
    )
    assert [r["path"] for r in edp.rank(repo, {})] == ["mod.py"]


def test_revert_commit_scores_higher_tier1_weight_than_fix_commit(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    repo = _repo(
        repo_dir,
        [
            ("feat: initial", {"reverted.py": "x = 1\n", "fixed.py": "y = 1\n"}),
            ('Revert "feat: broken change"', {"reverted.py": "x = 0\n"}),
            ("fix: off by one", {"fixed.py": "y = 2\n"}),
        ],
    )
    weights = dict(edp.fix_commits(repo))
    assert weights == {"reverted.py": edp.REVERT_WEIGHT, "fixed.py": edp.FIX_WEIGHT}

    ranked = edp.rank(repo, {})
    assert [row["path"] for row in ranked] == ["reverted.py", "fixed.py"]
    assert ranked[0]["tier1_escaped_defects"] == edp.REVERT_WEIGHT
    assert "revert commit touched this file" in ranked[0]["evidence"]


def test_capability_heartbeat_swallows_ledger_failure_without_blocking_rank(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated heartbeat failure")

    monkeypatch.setattr(capabilities, "production_heartbeat", boom)
    edp._capability_heartbeat()

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    repo = _repo(repo_dir, [("fix: bug", {"m.py": "x = 1\n"})])
    rows, status = edp.rank_status(repo, {})
    assert [row["path"] for row in rows] == ["m.py"]
    assert status["status"] == "ok"


def test_brain_signal_status_handles_missing_feedback_import_and_corrupt_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "feedback", None)
    missing_import = edp.brain_signal_status()
    assert missing_import == {
        "source": "git",
        "brain": "unknown",
        "reason": "feedback store not readable here",
    }

    schemaless = tmp_path / "schemaless.db"
    with contextlib.closing(sqlite3.connect(str(schemaless))) as conn:
        conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
        conn.commit()
    broken_query = edp.brain_signal_status(schemaless)
    assert broken_query["source"] == "git"
    assert broken_query["brain"] == "unknown"
    assert broken_query["reason"].startswith("query failed:")


def test_main_reports_valid_coverage_json_with_and_without_missing_statements(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    repo = _repo(repo_dir, [("fix: bug", {"m.py": "x = 1\n"})])

    cov_with_missing = tmp_path / "cov_missing.json"
    cov_with_missing.write_text(
        json.dumps({"files": {"m.py": {"summary": {"missing_lines": 7}}}}),
        encoding="utf-8",
    )
    assert edp.main(["--repo", str(repo), "--coverage-json", str(cov_with_missing), "--json"]) == 0
    with_missing_payload = json.loads(capsys.readouterr().out)
    assert with_missing_payload["tier3_coverage"] == "1 file(s) with missing statements"
    assert with_missing_payload["ranked"][0]["tier3_uncovered_statements"] == 7

    cov_all_clean = tmp_path / "cov_clean.json"
    cov_all_clean.write_text(
        json.dumps({"files": {"m.py": {"summary": {"missing_lines": 0}}}}),
        encoding="utf-8",
    )
    assert edp.main(["--repo", str(repo), "--coverage-json", str(cov_all_clean), "--json"]) == 0
    clean_payload = json.loads(capsys.readouterr().out)
    assert "UNAVAILABLE" in clean_payload["tier3_coverage"]
    assert "names no in-repo file with missing statements" in clean_payload["tier3_coverage"]
