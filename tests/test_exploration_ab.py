"""Historical exploration sizing and read-only simulation regressions."""

import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

import exploration_offline
import exploration_review
import feedback
import keepalive_outcomes
import route_weights_export
import router
import switch_review


@pytest.mark.parametrize(
    "run_id,source,mode,eligible",
    [
        ("policy-keepalive", "keepalive", "remote", True),
        ("policy-original", "orchestrator_remote", "local", True),
        ("policy-mode", "legacy", "remote", True),
        ("remote:o/r#1:old", "legacy", "local", True),
        ("policy-local", "legacy", "local", False),
    ],
)
def test_existing_remote_policy_stamping_matches_lookup(
    tmp_path, monkeypatch, run_id, source, mode, eligible
):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    feedback.record_run(
        run_id,
        "o/r#1",
        "implement",
        "codex",
        source=source,
        mode=mode,
        ts=100,
        routing_metadata={"original": "preserved"},
    )
    feedback.record_outcome(run_id, merged=True, durability="durable")
    assert keepalive_outcomes._existing_remote_for_pr("o/r", 1) == (run_id if eligible else None)
    keepalive_outcomes._stamp_existing_dispatch_policy(
        run_id,
        "o/r",
        1,
        "codex",
        lambda *_: [100],
        lambda _: {"policy_version": "dispatch-1"},
    )
    with feedback._conn() as c:
        row = c.execute(
            "SELECT r.ts,r.routing_metadata,o.durability FROM runs r JOIN outcomes o USING(run_id) WHERE run_id=?",
            (run_id,),
        ).fetchone()
    assert row is not None and row[0] == 100 and row[2] == "durable"
    metadata = json.loads(row[1])
    assert metadata.get("policy_version") == ("dispatch-1" if eligible else None)
    assert metadata["original"] == "preserved"


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
                _weight(c, version, ts, "claude", 0.995, 1000)  # reserve seat
                _weight(c, version, ts, "vibe", 0.2, 0)  # local-only seat
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

        # Ingest reuses several legacy remote-run forms. They must all replay
        # the same remote challenger pool instead of exploring a local seat.
        remote_runs = (
            ("keepalive-source", "keepalive", "local"),
            ("remote-source", "orchestrator_remote", "local"),
            ("remote-mode", "legacy", "remote"),
            ("remote:o/r#1:legacy", "legacy", "local"),
        )
        for run_id, source, mode in (*remote_runs, ("local", "legacy", "local")):
            feedback.record_run(
                run_id,
                "o/r#1",
                "implement",
                "codex",
                ts=250,
                source=source,
                mode=mode,
            )
            feedback.record_outcome(run_id, adjudicated_verdict="PASS")
        replay = exploration_offline.build_report(now=300, window_days=1)
        by_run = {row["run_id"]: row for row in replay["runs"]}
        for run_id, _, _ in remote_runs:
            result = by_run[run_id]
            assert result["route_weights_version"] == 2
            assert result["epsilon_challenger"] == "gemini"
            assert result["thompson_challenger"] == "codex"
            assert result["posterior_pass_difference"] == pytest.approx(0.8)
        assert by_run["local"]["epsilon_challenger"] == "vibe"
        assert replay["task_types"]["implement"]["compared_runs"] == 8


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


def test_export_carries_a_policy_version_and_one_sampled_challenger_per_task_type():
    with TemporaryDirectory() as tmp:
        database = Path(tmp) / "weights.db"
        route_weights_export._fixture_db(database)
        with sqlite3.connect(database) as c:
            c.execute("DELETE FROM route_weights")
            for task_type in route_weights_export.CONSUMER_TASK_TYPES:
                for agent, posterior, n_obs in (
                    ("cursor", 0.99, 1000),
                    ("codex", 0.1, 1),
                    ("gemini", 0.9, 1000),
                    ("claude", 1.0, 1000),  # reserve seats never become challengers
                    ("vibe", 0.01, 0),  # local seats never become challengers
                ):
                    c.execute(
                        "INSERT INTO route_weights "
                        "(version, ts, task_type, agent, posterior, score, n_obs, success_rate) "
                        "VALUES (1,100,?,?,?,?,?,?)",
                        (task_type, agent, posterior, posterior, n_obs, posterior),
                    )
        database_before = database.read_bytes()
        output = Path(tmp) / "export.json"
        policies = []
        with patch.object(router, "_capability_heartbeat") as heartbeat:
            for week, mode in ((2, "epsilon-greedy"), (3, "thompson-hybrid")):
                now = week * route_weights_export.WEEK_SECONDS + 100
                document = route_weights_export.build_document(database, now=now)
                assert document["schema"] == "orchestrator.route-weights/v1"
                exploration = document["exploration"]
                assert exploration["mode"] == mode
                assert exploration["rate"] == router.EXPLORATION_RATE_DEFAULT
                assert exploration["policy_version"].startswith("route-exploration/v1:")
                policies.append(exploration["policy_version"])
                assert set(exploration["challengers"]) == set(
                    route_weights_export.CONSUMER_TASK_TYPES
                )
                for task_type, challenger in exploration["challengers"].items():
                    allowed = {
                        row["agent"]
                        for row in router.ROUTE_TABLE[task_type]["agents"]
                        if not row["late"]
                    } & (router.KEEPALIVE_AGENTS - router.RESERVE_AGENTS - router.BACKUP_AGENTS)
                    assert challenger in allowed - {"cursor"}
                    expected = (
                        "gemini" if mode == "thompson-hybrid" and "gemini" in allowed else "codex"
                    )
                    assert challenger == expected
                assert route_weights_export.write_document(output, document) is True
                refreshed = route_weights_export.build_document(database, now=now + 86400)
                assert refreshed["exploration"] == exploration
                assert route_weights_export.write_document(output, refreshed) is False
            heartbeat.assert_not_called()
        assert policies[0] != policies[1]
        assert database.read_bytes() == database_before


def _exploration_run(run_id, mode, *, exploration=True, source="router_assignment", **outcome):
    import time

    feedback.record_run(
        run_id,
        "o/r#1",
        "implement",
        "codex",
        ts=int(time.time()),
        routing_metadata={
            "source": source,
            "exploration": exploration,
            "exploration_mode": mode,
        },
    )
    if outcome:
        fields = list(outcome)
        with feedback._conn() as c:
            c.execute(
                f"INSERT INTO outcomes (run_id, {', '.join(fields)}) "
                f"VALUES ({', '.join('?' for _ in range(len(fields) + 1))})",
                [run_id, *outcome.values()],
            )


def _switch_report(gate):
    return switch_review.format_report(
        {
            "review_days": switch_review.REVIEW_DAYS,
            "raise_count": 0,
            "due": [],
            "held_off": [],
            "on_but_idle": [],
            "unconditioned": [],
            "exploration_gate": gate,
        }
    )


def test_switch_review_reports_grades_and_durability_separately(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    epsilon = "epsilon-greedy"
    _exploration_run("pending-pass", epsilon, adjudicated_verdict="PASS", durability="pending")
    _exploration_run("reverted-pass", epsilon, adjudicated_verdict="PASS", durability="reverted")
    _exploration_run(
        "verifier-fail",
        epsilon,
        adjudicated_verdict="PASS",
        verifier_verdict="NON_PASS",
        durability="durable",
    )
    _exploration_run("swept-only", epsilon, durability="durable")
    _exploration_run("verifier-pass", epsilon, verifier_verdict="PASS", durability="pending")
    _exploration_run(
        "infra",
        epsilon,
        adjudicated_verdict="PASS",
        durability="durable",
        failure_class="transient_infra",
    )
    _exploration_run("waiting", epsilon)
    _exploration_run("exploitation", epsilon, exploration=False, adjudicated_verdict="PASS")
    _exploration_run("unattributed", epsilon, source="keepalive", adjudicated_verdict="PASS")
    _exploration_run(
        "thompson-fail", "thompson-hybrid", adjudicated_verdict="FAIL", durability="pending"
    )
    with patch.object(router, "_capability_heartbeat") as heartbeat:
        gate = switch_review._exploration_gate()
        heartbeat.assert_not_called()
    by_mode = {row["mode"]: row for row in gate["arms"]}
    arm = by_mode[epsilon]
    assert arm["runs"] == 7
    assert arm["graded_runs"] == 4
    assert arm["pass_runs"] == 3
    assert arm["pass_rate"] == 3 / 4
    assert arm["durability_runs"] == 3
    assert arm["durable_runs"] == 2
    assert arm["durable_rate"] == 2 / 3
    text = _switch_report(gate)
    assert "window=120d" in text
    assert "PASS denominator=graded G" in text
    assert "durable denominator=completed durability sweeps D" in text
    assert (
        "epsilon-greedy: exploration decisions N=7 graded G=4 PASS=75.0% durable=66.7% D=3" in text
    )
    assert (
        "thompson-hybrid: exploration decisions N=1 graded G=1 PASS=0.0% durable=unmeasured D=0"
        in text
    )


def test_switch_review_empty_arms_have_unmeasured_percentages(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    with feedback._conn():
        pass
    text = _switch_report(switch_review._exploration_gate())
    for mode in ("epsilon-greedy", "thompson-hybrid"):
        assert (
            f"{mode}: exploration decisions N=0 graded G=0 PASS=unmeasured durable=unmeasured D=0"
            in text
        )


def test_switch_review_unreadable_arms_do_not_report_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path)
    gate = switch_review._exploration_gate()
    assert gate["suspect"] is True
    text = _switch_report(gate)
    assert "SUSPECT — direct_mode_evidence_unreadable" in text
    assert "exploration decisions N=" not in text
