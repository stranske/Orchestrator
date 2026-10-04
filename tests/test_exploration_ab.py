"""Historical exploration sizing and read-only simulation regressions."""

import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import exploration_offline
import exploration_review
import feedback
import router


def _weight(c, version, ts, agent, posterior, n_obs, task_type="implement"):
    c.execute(
        "INSERT INTO route_weights "
        "(version, ts, task_type, agent, posterior, score, n_obs) VALUES (?,?,?,?,?,?,?)",
        (version, ts, task_type, agent, posterior, posterior, n_obs),
    )


def _run(run_id, ts, task_type="implement", verdict="PASS", failure_class=None):
    feedback.record_run(
        run_id, "o/r#1", task_type, "codex", ts=ts, source="keepalive", mode="remote"
    )
    with feedback._conn() as c:
        c.execute(
            "INSERT INTO outcomes (run_id, adjudicated_verdict, failure_class) VALUES (?,?,?)",
            (run_id, verdict, failure_class),
        )


def test_offline_sizing_uses_the_version_in_force_at_each_run():
    with TemporaryDirectory() as tmp, patch.object(feedback, "DB_PATH", Path(tmp) / "brain.db"):
        with feedback._conn() as c:
            for version, ts, bad, good in (
                (1, 100, "codex", "gemini"),
                (2, 200, "gemini", "codex"),
                (3, 400, "codex", "gemini"),  # future weights must never enter the replay
            ):
                _weight(c, version, ts, "cursor", 0.99, 1000)
                _weight(c, version, ts, bad, 0.1, 1)
                _weight(c, version, ts, good, 0.9, 1000)
        _run("before-history", 99)
        _run("old", 150)
        _run("at-boundary", 200)
        _run("new", 250)
        database_before = feedback.DB_PATH.read_bytes()
        report = exploration_offline.build_report(now=300, window_days=1)
        by_run = {row["run_id"]: row for row in report["runs"]}
        assert by_run["before-history"]["route_weights_version"] is None
        assert by_run["before-history"]["skip_reason"] == "no_weights_in_force"
        assert by_run["old"]["route_weights_version"] == 1
        assert by_run["old"]["epsilon_challenger"] == "codex"
        assert by_run["old"]["thompson_challenger"] == "gemini"
        assert by_run["at-boundary"]["route_weights_version"] == 2
        assert by_run["new"]["route_weights_version"] == 2
        assert by_run["new"]["epsilon_challenger"] == "gemini"
        assert by_run["new"]["thompson_challenger"] == "codex"
        for name in ("old", "at-boundary", "new"):
            assert abs(by_run[name]["posterior_pass_difference"] - 0.8) < 1e-12
        stat = report["task_types"]["implement"]
        assert stat["graded_runs"] == 4
        assert stat["compared_runs"] == stat["different_challengers"] == 3
        assert stat["skipped_runs"] == 1
        assert abs(stat["expected_pass_difference"] - 0.12) < 1e-12
        assert "sizing estimate" in exploration_offline.format_human(report)
        assert exploration_offline.build_report(now=300, window_days=1) == report
        assert feedback.DB_PATH.read_bytes() == database_before


def test_offline_sizing_excludes_ungraded_infra_and_outside_window_runs():
    with TemporaryDirectory() as tmp, patch.object(feedback, "DB_PATH", Path(tmp) / "brain.db"):
        with feedback._conn() as c:
            _weight(c, 1, 100, "cursor", 0.99, 1000)
            _weight(c, 1, 100, "codex", 0.1, 1)
            _weight(c, 1, 100, "gemini", 0.9, 1000)
        _run("graded", 99000)
        _run("ungraded", 99000, verdict=None)
        _run("infra", 99000, failure_class=next(iter(feedback.LEARNING_EXCLUDED_FAILURE_CLASSES)))
        _run("too-old", 1000)
        _run("future-run", 100001)
        _run("unknown-task", 99000, task_type="unknown")
        report = exploration_offline.build_report(now=100000, window_days=1)
        assert {row["run_id"] for row in report["runs"]} == {"graded", "unknown-task"}
        assert report["task_types"]["unknown"]["skipped_runs"] == 1


def test_offline_sizing_does_not_create_a_missing_database():
    with TemporaryDirectory() as tmp:
        database = Path(tmp) / "absent.db"
        try:
            exploration_offline.build_report(db_path=database)
        except sqlite3.OperationalError as exc:
            assert "unable to open database" in str(exc)
        else:
            raise AssertionError("missing history must fail visibly")
        assert not database.exists()


def test_review_simulations_do_not_heartbeat():
    with TemporaryDirectory() as tmp, patch.object(feedback, "DB_PATH", Path(tmp) / "brain.db"):
        with feedback._conn() as c:
            _weight(c, 1, 100, "cursor", 0.99, 1000)
            _weight(c, 1, 100, "codex", 0.1, 1)
            _weight(c, 1, 100, "gemini", 0.9, 1000)
        with patch.object(router, "_capability_heartbeat") as heartbeat:
            exploration_review.build_report(
                route_table={"implement": router.ROUTE_TABLE["implement"]}, version=1, trials=10
            )
            _run("old", 150)
            exploration_offline.build_report(now=300, window_days=1)
            heartbeat.assert_not_called()
            router.select_agent(
                "implement",
                exploration_review._neutral_capacity(),
                exploration_rate=1.0,
                exploration_mode="thompson-hybrid",
            )
            heartbeat.assert_called_once()
            assert heartbeat.call_args.args == ("thompson-hybrid-routing", "invocation")
