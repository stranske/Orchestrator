"""Behavioral checks for issue #1229's ranked coverage targets.

Runtime paths and feedback connections are per-test temporary state. Network
and process creation are denied by default. Two git integration tests opt into
local-only git in tmp_path, with five-second command timeouts and a launch cap.
Measured coverage and per-test mutation evidence are recorded on PR #1230.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sqlite3
import subprocess
import tempfile
from pathlib import Path

import pytest

import feedback
import langsmith_fetch
import objective_anchor
import outcomes
import provision


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Deny external I/O and put runtime, temp files and DBs under tmp_path."""
    for key in ("ORCH_STATE_DIR", "ORCH_LOCAL_RUNTIME"):
        monkeypatch.setenv(key, str(tmp_path))
    monkeypatch.setenv("ORCH_FEEDBACK_DB", str(tmp_path / "feedback.db"))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(objective_anchor, "EXP_DIR", tmp_path / "exps")
    monkeypatch.setattr(provision, "REPOS_DIR", tmp_path / "repos")
    connections = []
    real_conn = feedback._conn

    def temporary_conn(**kwargs):
        assert Path(feedback.DB_PATH).resolve().is_relative_to(tmp_path.resolve())
        conn = real_conn(**kwargs)
        connections.append(conn)
        return conn

    monkeypatch.setattr(feedback, "_conn", temporary_conn)
    forbidden_calls = []

    def forbidden(*args, **kwargs):
        forbidden_calls.append((args, kwargs))
        raise AssertionError(f"unexpected network/process launch: {args} {kwargs}")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    yield
    for conn in connections:
        conn.close()
    assert forbidden_calls == []


# Save the real entry points before the autouse fixture denies process creation.
_REAL_RUN = subprocess.run
_REAL_POPEN = subprocess.Popen


@pytest.fixture
def bounded_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Allow only local fixture git operations, at most 20 launches, 5s each."""
    calls = []
    denied = []
    allowed = {"init", "config", "add", "commit", "branch", "update-ref", "rev-parse", "merge-base"}
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        HOME=str(tmp_path),
        XDG_CONFIG_HOME=str(tmp_path),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GIT_ALLOW_PROTOCOL="",
    )

    def popen(cmd, **kwargs):
        try:
            assert not kwargs.get("shell")
            assert cmd[0] == "git"
            args = list(cmd[1:])
            while args and args[0] == "-c":
                assert args[1] in {"user.email=t@t", "user.name=t"}
                args = args[2:]
            assert args[0] in allowed
            assert Path(kwargs["cwd"]).resolve().is_relative_to(tmp_path.resolve())
            assert len(calls) < 20
        except AssertionError:
            denied.append(cmd)
            raise
        calls.append(cmd)
        kwargs["env"] = env
        return _REAL_POPEN(cmd, **kwargs)

    def run(cmd, **kwargs):
        kwargs["timeout"] = 5
        return _REAL_RUN(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(subprocess, "run", run)
    yield calls
    assert denied == []


# =============================================================================
# objective_anchor.py coverage
# =============================================================================


class TestObjectiveAnchorExistingAnchorRefs:
    """Cover _existing_anchor_refs() function (lines 201-207)."""

    def test_empty_table(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Cover lines 202-206: exception path returns empty set."""
        db_path = tmp_path / "feedback.db"
        monkeypatch.setattr(feedback, "DB_PATH", db_path)
        # Bypass _conn's schema creation to exercise the missing-table exception.
        monkeypatch.setattr(feedback, "_conn", lambda: contextlib.closing(sqlite3.connect(db_path)))
        result = objective_anchor._existing_anchor_refs()
        assert result == set()

    def test_with_rows(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Cover lines 202-207: reads rows from DB."""
        import sqlite3

        db_path = tmp_path / "feedback.db"
        # Create and populate the DB
        with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
            conn.execute("CREATE TABLE human_calibration (id INTEGER PRIMARY KEY, ref TEXT)")
            conn.execute(
                "INSERT INTO human_calibration (ref) VALUES ('exp1:agent1'), ('exp2:agent2')"
            )
            conn.commit()

        # Mock the _conn function to use our test DB
        def mock_conn(*, timeout=5.0):
            return contextlib.closing(sqlite3.connect(str(db_path), timeout=timeout))

        monkeypatch.setattr(feedback, "_conn", mock_conn)
        result = objective_anchor._existing_anchor_refs()
        assert result == {"exp1:agent1", "exp2:agent2"}

    def test_with_null_rows(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Cover line 207: filter out null/empty refs."""
        import sqlite3

        db_path = tmp_path / "feedback.db"
        with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
            conn.execute("CREATE TABLE human_calibration (id INTEGER PRIMARY KEY, ref TEXT)")
            conn.execute("INSERT INTO human_calibration (ref) VALUES (NULL), ('exp1:agent1'), ('')")
            conn.commit()

        def mock_conn(*, timeout=5.0):
            return contextlib.closing(sqlite3.connect(str(db_path), timeout=timeout))

        monkeypatch.setattr(feedback, "_conn", mock_conn)
        result = objective_anchor._existing_anchor_refs()
        assert result == {"exp1:agent1"}


class TestObjectiveAnchorArmBase:
    """Cover _arm_base() function (lines 180-198).

    Uncovered lines from issue #1229: 188-189, 192-193, 195-196
    """

    def test_missing_repo_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Cover lines 188-189: repo_dir doesn't exist."""
        repos_dir = tmp_path / "repos"
        repos_dir.mkdir()
        monkeypatch.setattr(provision, "REPOS_DIR", repos_dir)
        result = objective_anchor._arm_base("missing/repo", "main", "EXP1", "agent1")
        assert result is None

    def test_missing_branch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Cover lines 192-193: branch doesn't exist."""
        repos_dir = tmp_path / "repos"
        repo_path = repos_dir / "test__repo"
        repo_path.mkdir(parents=True)
        monkeypatch.setattr(provision, "REPOS_DIR", repos_dir)
        calls = []

        def missing_branch(cmd, cwd):
            calls.append((cmd, cwd))
            return subprocess.CompletedProcess(cmd, 1, "", "missing branch")

        monkeypatch.setattr(objective_anchor, "_run", missing_branch)
        result = objective_anchor._arm_base("test/repo", "main", "EXP1", "missing")
        assert result is None
        assert calls == [
            (["git", "rev-parse", "--verify", "--quiet", "exp/EXP1-missing"], repo_path)
        ]

    def test_branch_exists(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bounded_git):
        """Cover lines 181-198: branch exists, merge-base succeeds."""
        repos_dir = tmp_path / "repos"
        repo_path = repos_dir / "test__repo"
        repo_path.mkdir(parents=True)
        monkeypatch.setattr(provision, "REPOS_DIR", repos_dir)

        # Initialize a git repo
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_path, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo_path, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_path, check=True)
        (repo_path / "file.txt").write_text("content")
        subprocess.run(["git", "add", "."], cwd=repo_path, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo_path, check=True)

        # Create exp branch
        subprocess.run(["git", "branch", "exp/EXP1-agent1"], cwd=repo_path, check=True)
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=repo_path, check=True
        )

        expected_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        result = objective_anchor._arm_base("test/repo", "main", "EXP1", "agent1")
        assert result == expected_sha
        assert len(bounded_git) == 10


class TestObjectiveAnchorLatestExpIds:
    """Cover _latest_exp_ids() function (lines 286-289).

    Uncovered lines from issue #1229: 287-289
    """

    def test_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Cover lines 287-289: no experiment directories."""
        exp_dir = tmp_path / "exps"
        exp_dir.mkdir()  # Create the directory
        monkeypatch.setattr(objective_anchor, "EXP_DIR", exp_dir)
        result = objective_anchor._latest_exp_ids(10)
        assert result == []

    def test_with_exps(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Cover lines 287-289: with experiment directories."""
        exp_dir = tmp_path / "exps"
        for i in range(3):
            (exp_dir / f"exp{i}").mkdir(parents=True)
            (exp_dir / f"exp{i}" / "meta.json").write_text('{"repo": "test/repo"}')
            stamp = 1_000_000 + i * 10
            os.utime((exp_dir / f"exp{i}"), (stamp, stamp))
            os.utime((exp_dir / f"exp{i}" / "meta.json"), (stamp, stamp))
        monkeypatch.setattr(objective_anchor, "EXP_DIR", exp_dir)
        result = objective_anchor._latest_exp_ids(2)
        assert len(result) == 2
        assert result[0] == "exp2"
        assert result[1] == "exp1"


class TestObjectiveAnchorMain:
    """Cover main() CLI paths (lines 427-464).

    Uncovered lines from issue #1229: 386, 440-449, 451-452, 456-460
    """

    def test_no_args(self):
        """Cover line 446: no exp_id or latest args."""
        with pytest.raises(SystemExit) as exc_info:
            objective_anchor.main([])
        assert exc_info.value.code == 2

    def test_selftest(self, capsys, bounded_git):
        """Cover lines 438-439: --selftest path."""
        result = objective_anchor.main(["--selftest"])
        assert result == 0
        captured = capsys.readouterr()
        assert "selftest: OK" in captured.out

    def test_json_output(self, tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch):
        """Cover lines 448-450: --json output path."""
        exp_dir = tmp_path / "exps"
        (exp_dir / "EXP1").mkdir(parents=True)
        (exp_dir / "EXP1" / "meta.json").write_text(
            json.dumps({"repo": "o/r", "base": "main", "agents": []})
        )
        monkeypatch.setattr(objective_anchor, "EXP_DIR", exp_dir)
        monkeypatch.setattr(
            objective_anchor,
            "anchor_experiment",
            lambda exp_id, **kwargs: {"exp_id": exp_id, "anchored": [], "skipped": []},
        )
        result = objective_anchor.main(["--exp-id", "EXP1", "--json"])
        assert result == 0
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert len(output) == 1
        assert output[0]["exp_id"] == "EXP1"

    def test_dry_run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Cover lines 447, 455: --dry-run path."""
        exp_dir = tmp_path / "exps"
        (exp_dir / "EXP1").mkdir(parents=True)
        (exp_dir / "EXP1" / "meta.json").write_text(
            json.dumps({"repo": "o/r", "base": "main", "agents": []})
        )
        called = []
        monkeypatch.setattr(objective_anchor, "EXP_DIR", exp_dir)
        monkeypatch.setattr(
            objective_anchor,
            "anchor_experiment",
            lambda exp_id, **kwargs: (
                called.append(kwargs.get("apply", True)),
                {"exp_id": exp_id, "anchored": [], "skipped": []},
            )[1],
        )
        result = objective_anchor.main(["--exp-id", "EXP1", "--dry-run"])
        assert result == 0
        assert len(called) == 1
        assert called[0] is False

    def test_latest_n(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Cover lines 443-444, 447: --latest N path."""
        exp_dir = tmp_path / "exps"
        for i in range(3):
            (exp_dir / f"exp{i}").mkdir(parents=True)
            (exp_dir / f"exp{i}" / "meta.json").write_text(
                json.dumps({"repo": "o/r", "base": "main", "agents": []})
            )
        call_ids = []
        monkeypatch.setattr(objective_anchor, "EXP_DIR", exp_dir)
        monkeypatch.setattr(
            objective_anchor,
            "anchor_experiment",
            lambda exp_id, **kwargs: (
                call_ids.append(exp_id),
                {"exp_id": exp_id, "anchored": [], "skipped": []},
            )[1],
        )
        result = objective_anchor.main(["--latest", "2"])
        assert result == 0
        assert len(call_ids) == 2

    def test_normal_output(self, tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch):
        """Cover lines 451-452, 456-460: normal text output path."""
        exp_dir = tmp_path / "exps"
        (exp_dir / "EXP1").mkdir(parents=True)
        (exp_dir / "EXP1" / "meta.json").write_text(
            json.dumps({"repo": "o/r", "base": "main", "agents": []})
        )
        monkeypatch.setattr(objective_anchor, "EXP_DIR", exp_dir)
        monkeypatch.setattr(
            objective_anchor,
            "anchor_experiment",
            lambda exp_id, **kwargs: {
                "exp_id": exp_id,
                "anchored": [{"agent": "t", "score": 8.0, "label": "g"}],
                "skipped": [{"agent": "t2", "reason": "b"}],
            },
        )
        result = objective_anchor.main(["--exp-id", "EXP1"])
        assert result == 0
        captured = capsys.readouterr()
        assert "EXP1" in captured.out
        assert "anchored=1" in captured.out
        assert "skipped=1" in captured.out


class TestObjectiveAnchorScoreFromSignals:
    """Cover score_from_signals() function (lines 53-71).

    These are pure functions that are easy to test.
    """

    def test_apply_fail(self):
        """Cover line 61: apply-fail."""
        score, label = objective_anchor.score_from_signals({"applies": False})
        assert score == 1.0
        assert label == "apply-fail"

    def test_apply_none_not_decisive(self):
        """Cover lines 58-59: applies=None is not decisive."""
        result = objective_anchor.score_from_signals({"applies": None, "detail": "no clone"})
        assert result is None

    def test_compile_fail(self):
        """Cover lines 62-63: compile-fail."""
        score, label = objective_anchor.score_from_signals({"applies": True, "compile_ok": False})
        assert score == 2.0
        assert label == "compile-fail"

    def test_no_probe_not_decisive(self):
        """Cover lines 64-66, 68: no probe or probe=None is not decisive."""
        assert (
            objective_anchor.score_from_signals(
                {"applies": True, "compile_ok": True, "probe": None}
            )
            is None
        )
        assert objective_anchor.score_from_signals({"applies": True, "probe": None}) is None

    def test_tests_fail(self):
        """Cover lines 65-70: tests-fail."""
        score, label = objective_anchor.score_from_signals(
            {"applies": True, "probe": {"patched_pass": False, "base_pass": False}}
        )
        assert score == 2.5
        assert label == "tests-fail"

    def test_hollow_tests(self):
        """Cover lines 69-70: hollow-tests."""
        score, label = objective_anchor.score_from_signals(
            {"applies": True, "probe": {"patched_pass": True, "base_pass": True}}
        )
        assert score == 3.5
        assert label == "hollow-tests"

    def test_tests_pass_break_verified(self):
        """Cover lines 70-71: tests-pass-break-verified."""
        score, label = objective_anchor.score_from_signals(
            {"applies": True, "probe": {"patched_pass": True, "base_pass": False}}
        )
        assert score == 8.0
        assert label == "tests-pass-break-verified"


class TestObjectiveAnchorChangedFiles:
    """Cover _changed_files() function (lines 78-85)."""

    def test_simple(self):
        """Cover lines 79-85: parse changed files from patch."""
        result = objective_anchor._changed_files(
            "+++ b/a.py\n+++ b/tests/test_a.py\n+++ /dev/null\n"
        )
        assert result == ["a.py", "tests/test_a.py"]

    def test_empty(self):
        """Cover line 85: empty result."""
        result = objective_anchor._changed_files("no changed files\n")
        assert result == []

    def test_dev_null_only(self):
        """Cover line 84: /dev/null is filtered out."""
        result = objective_anchor._changed_files("+++ /dev/null\n")
        assert result == []


# =============================================================================
# langsmith_fetch.py coverage
# =============================================================================


class TestLangsmithFetchEnvInt:
    """Cover _env_int() function (lines 64-72).

    Uncovered lines from issue #1229: 68-72
    """

    def test_default_value(self, monkeypatch: pytest.MonkeyPatch):
        """Cover lines 65-66: default value when env var not set."""
        monkeypatch.delenv("TEST_ENV_VAR", raising=False)
        result = langsmith_fetch._env_int("TEST_ENV_VAR", 10)
        assert result == 10

    def test_valid_int(self, monkeypatch: pytest.MonkeyPatch):
        """Cover lines 68-72: valid int value."""
        monkeypatch.setenv("TEST_ENV_VAR", "5")
        result = langsmith_fetch._env_int("TEST_ENV_VAR", 10, minimum=1)
        assert result == 5

    def test_invalid_int(self, monkeypatch: pytest.MonkeyPatch):
        """Cover lines 68-71: invalid int value falls back to default."""
        monkeypatch.setenv("TEST_ENV_VAR", "not_a_number")
        result = langsmith_fetch._env_int("TEST_ENV_VAR", 10)
        assert result == 10

    def test_below_minimum(self, monkeypatch: pytest.MonkeyPatch):
        """Cover line 72: value below minimum uses minimum."""
        monkeypatch.setenv("TEST_ENV_VAR", "0")
        result = langsmith_fetch._env_int("TEST_ENV_VAR", 10, minimum=5)
        assert result == 5


class TestLangsmithFetchRepoSlug:
    """Cover _repo_slug() function (lines 85-86)."""

    def test_simple(self):
        """Cover line 86: simple repo slug conversion."""
        result = langsmith_fetch._repo_slug("owner/repo")
        assert result == "owner__repo"

    def test_with_colon(self):
        """Cover line 86: repo with colon."""
        result = langsmith_fetch._repo_slug("owner/repo:branch")
        assert result == "owner__repo_branch"

    def test_no_special_chars(self):
        """Cover line 86: repo with no special chars."""
        result = langsmith_fetch._repo_slug("repo")
        assert result == "repo"


# =============================================================================
# outcomes.py coverage
# =============================================================================


class TestOutcomesIsLocalDelegate:
    """Cover is_local_delegate() function (lines 235-237).

    Line 259 from issue #1229 is in _pr_state, but we can cover simpler functions first.
    """

    def test_local_mode(self):
        """Cover line 237: mode is 'local'."""
        result = outcomes.is_local_delegate("local", "owner/repo#123")
        assert result is True

    @pytest.mark.parametrize("mode", outcomes.LEGACY_LOCAL_MODES)
    @pytest.mark.parametrize(
        "target, expected", [("owner/repo#123", True), ("owner/repo", False), (None, False)]
    )
    def test_legacy_mode_with_hash(self, mode, target, expected):
        """Every actual legacy mode requires a numbered target."""
        assert outcomes.is_local_delegate(mode, target) is expected

    @pytest.mark.parametrize("target", ["owner/repo#123", "owner/repo", None])
    def test_non_local_mode(self, target):
        """Remote mode never counts as a local delegation."""
        assert outcomes.is_local_delegate("remote", target) is False


class TestOutcomesNeedsDelegationGuard:
    """Only the remote-delegation source needs the guard."""

    @pytest.mark.parametrize(
        "source, expected",
        [(outcomes.DELEGATION_SOURCE, True), ("github", False), ("local", False)],
    )
    def test_remote_source(self, source, expected):
        assert outcomes.needs_delegation_guard(source) is expected

    def test_none_source(self):
        assert outcomes.needs_delegation_guard(None) is False


class TestOutcomesPrState:
    """Cover _pr_state() function (lines 250-259).

    Line 259 is uncovered: invalid target with no number.
    """

    def test_invalid_target(self):
        """Cover line 259: num is None."""
        result = outcomes._pr_state("owner/repo", agent=None)
        assert result == {"lookup_status": "invalid_target", "target": "owner/repo"}
