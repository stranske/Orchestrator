"""Bounded behavioral coverage for issue #1207; evidence is recorded in PR #1208."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import tempfile
from pathlib import Path

import pytest

import claims
import features
import feedback
import human_calibration
import research_scheduler


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every mutable registry and claim under this test's temporary runtime."""
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    state.mkdir()
    runtime.mkdir()
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(runtime))
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setenv("ORCH_FEATURES_PATH", str(runtime / "features.json"))
    monkeypatch.setattr(feedback, "DB_PATH", runtime / "feedback.db")
    monkeypatch.setattr(feedback, "_capability_daily_heartbeat", lambda *a, **k: None)
    attempted: list[tuple] = []

    def forbidden_subprocess_or_socket(*args, **kwargs):
        attempted.append((args, kwargs))
        raise AssertionError("issue1207 ranked coverage must not start subprocess or network work")

    for name in ("run", "Popen"):
        monkeypatch.setattr(subprocess, name, forbidden_subprocess_or_socket)
    for name in ("socket", "create_connection"):
        monkeypatch.setattr(socket, name, forbidden_subprocess_or_socket)
    monkeypatch.setattr("capabilities.production_heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr(features, "REG", runtime / "features.json")
    yield
    assert attempted == [], f"unexpected external entry attempts: {attempted}"


class DroppedCopyCursor(sqlite3.Cursor):
    """Simulate a storage fault that silently copies zero rows during a rebuild."""

    def execute(self, sql, parameters=()):
        if sql.startswith("INSERT INTO evaluations_v2_rebuild") or sql.startswith(
            "INSERT INTO agent_switches_rebuild"
        ):
            return super().execute("SELECT 1 WHERE 0")
        return super().execute(sql, parameters)


class DroppedCopyConnection(sqlite3.Connection):
    def cursor(self, *args, **kwargs):
        return super().cursor(factory=DroppedCopyCursor)

    def execute(self, sql, parameters=()):
        return self.cursor().execute(sql, parameters)


@pytest.mark.parametrize("drop_copy", [False, True], ids=["preserved", "row-loss"])
def test_feedback_migrate_schema_evaluations_v2_row_loss(drop_copy) -> None:
    factory = DroppedCopyConnection if drop_copy else sqlite3.Connection
    with sqlite3.connect(":memory:", factory=factory) as conn:
        conn.execute(
            "CREATE TABLE evaluations_v2 (experiment_id TEXT, implementer_arm TEXT, "
            "profile_id TEXT, evaluator_arm TEXT, score REAL, rank INTEGER, verdict TEXT, ts INTEGER)"
        )
        conn.execute("INSERT INTO evaluations_v2 VALUES ('e1','a1','p1','j1',8.5,1,'pass',1000)")
        original = conn.execute("SELECT * FROM evaluations_v2").fetchall()
        conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, ts INTEGER)")
        if drop_copy:
            with pytest.raises(
                RuntimeError, match="evaluations_v2 migration refused row loss: before=1 after=0"
            ):
                feedback._migrate_schema(conn)
            assert conn.execute("SELECT * FROM evaluations_v2").fetchall() == original
        else:
            feedback._migrate_schema(conn)
            assert conn.execute("SELECT * FROM evaluations_v2").fetchall() == [
                ("e1", "a1", "a1", "p1", None, "j1", "j1", None, None, 8.5, 1, "pass", 1000)
            ]


@pytest.mark.parametrize("drop_copy", [False, True], ids=["preserved", "row-loss"])
def test_feedback_widen_agent_switches(drop_copy) -> None:
    factory = DroppedCopyConnection if drop_copy else sqlite3.Connection
    with sqlite3.connect(":memory:", factory=factory) as conn:
        conn.execute(
            "CREATE TABLE agent_switches (pr_ref TEXT, from_agent TEXT, to_agent TEXT, "
            "switched_ts INTEGER, auto_label INTEGER, commits_before INTEGER, commits_after INTEGER, "
            "merged INTEGER, durability TEXT, recorded_ts INTEGER, PRIMARY KEY (pr_ref, switched_ts))"
        )
        row = ("o/r#1", "a", "b", 1, 0, 1, 2, 1, "durable", 3)
        conn.execute("INSERT INTO agent_switches VALUES (?,?,?,?,?,?,?,?,?,?)", row)
        conn.commit()
        if drop_copy:
            with pytest.raises(
                RuntimeError, match="agent_switches migration refused row loss: before=1 after=0"
            ):
                feedback._widen_agent_switches(conn)
            assert not conn.in_transaction
            assert conn.execute("SELECT * FROM agent_switches").fetchall() == [row]
        else:
            feedback._widen_agent_switches(conn)
            assert conn.execute(
                "SELECT pr_ref,from_agent,to_agent,switched_ts,auto_label,commits_before,"
                "commits_after,merged,durability,recorded_ts,source,delegation_source FROM agent_switches"
            ).fetchall() == [(*row, "label", None)]


def test_feedback_selftest_resolve_db_path(tmp_path, monkeypatch) -> None:
    # The focused selftest exercises its own finally; observe both missing and existing keys.
    monkeypatch.delenv(feedback.DB_PATH_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "resolve-selftest"
    root.mkdir()
    monkeypatch.setattr(tempfile, "mkdtemp", lambda **kwargs: str(root))
    keys = (feedback.DB_PATH_ENV, "ORCH_LOCAL_RUNTIME", "ORCH_STATE_DIR", "HOME")
    before = {key: os.environ.get(key) for key in keys}
    default = feedback.DB_PATH
    feedback._selftest_resolve_db_path()
    assert {key: os.environ.get(key) for key in keys} == before
    assert feedback.DB_PATH == default


def test_feedback_influence_edge_unlinked() -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.executescript(feedback.SCHEMA)
        conn.execute("INSERT INTO runs (run_id,ts) VALUES ('orphan',0)")
        args = dict(
            target_run_id="orphan", influence_type="role", influence_id="probe", accepted=True
        )
        with pytest.raises(ValueError, match="no completion event"):
            feedback._record_influence_edge_in_conn(conn, **args)
        assert conn.execute("SELECT COUNT(*) FROM influence_edges").fetchone() == (0,)
        feedback._record_influence_edge_in_conn(conn, **args, allow_unlinked=True)
        assert conn.execute(
            "SELECT target_run_id,target_event_id FROM influence_edges"
        ).fetchall() == [("orphan", None)]


def test_human_calibration_build_anchor_payload() -> None:
    payload = human_calibration.build_anchor_payload(
        experiment_id=" exp ", implementer=" member ", score="7", member_id="member", arm_id="arm"
    )
    assert payload["ref"] == "exp:member"
    assert json.loads(payload["human_verdict"]) == {
        "experiment_id": "exp",
        "implementer": "member",
        "score": 7.0,
        "member_id": "member",
        "arm_id": "arm",
    }
    with pytest.raises(human_calibration.CalibrationInputError, match="member_id must match"):
        human_calibration.build_anchor_payload(
            experiment_id="exp", implementer="a", score=7, member_id="b"
        )


def test_human_calibration_parse_human_anchors() -> None:
    rows = [
        (
            1,
            "exp:codex",
            json.dumps({"experiment_id": "exp", "implementer": "codex", "score": 8}),
            "note",
        )
    ]
    assert human_calibration.parse_human_anchors(rows) == [
        {
            "ts": 1,
            "ref": "exp:codex",
            "experiment_id": "exp",
            "implementer": "codex",
            "score": 8.0,
            "note": "note",
        }
    ]


def test_human_calibration_calibrate_score() -> None:
    assert human_calibration.calibrate_score(5.0, None) == 5.0
    assert human_calibration.calibrate_score(5.0, {"ready": False, "slope": 0}) == 5.0
    assert (
        human_calibration.calibrate_score(5.0, {"ready": True, "intercept": 1, "slope": 0.5}) == 3.5
    )
    assert (
        human_calibration.calibrate_score(9.0, {"ready": True, "intercept": 1, "slope": 2})
        == feedback.QUALITY_MAX
    )


def test_human_calibration_main(capsys) -> None:
    assert human_calibration.main(["--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ready"] is False
    assert report["matched_pair_count"] == 0
    assert report["window_days"] == 365


def test_features_validate_registry() -> None:
    with pytest.raises(ValueError, match="must be an object"):
        features._validate_registry([])
    with pytest.raises(ValueError, match="named objects"):
        features._validate_registry({1: {}})


def test_features_record_use(tmp_path) -> None:
    path = tmp_path / "registry.json"
    first = features.record_use("test_feature", "first", path=path)
    assert (first["maturity"], first["uses"], first["count"]) == ("ad-hoc", ["first"], 1)
    features.record_use("test_feature", "first", path=path)
    second = features.record_use("test_feature", "second", path=path)
    assert (second["maturity"], second["uses"], second["count"]) == (
        "reused",
        ["first", "second"],
        2,
    )
    assert json.loads(path.read_text())["test_feature"] == second


def test_features_summary(tmp_path) -> None:
    path = tmp_path / "registry.json"
    # Seed an empty registry so totals refer only to these observed uses.
    path.write_text("{}")
    for where in ("one", "two", "three"):
        features.record_use("f1", where, path=path)
    report = features.summary(path)
    assert report["total"] == 1
    assert report["counts_by_maturity"] == {"ad-hoc": 0, "reused": 1, "hardened": 0}
    assert report["promotion_candidates"] == [{"name": "f1", "count": 3, "problem": ""}]


def test_research_scheduler_select_arms() -> None:
    arms = research_scheduler.select_arms(
        ["busy", "leader", "uncertain", "settled"],
        {"busy": 1.0, "leader": 0.9, "uncertain": 0.5, "settled": 0.1},
        {"busy": 0, "leader": 1, "uncertain": 1, "settled": 1},
        max_n=2,
    )
    assert arms == ["leader", "uncertain"]
    assert research_scheduler.select_arms(["busy", "leader"], {}, {"leader": 1}) == ["leader"]


def test_claims_claim_and_release(capsys) -> None:
    target = "owner/repo#1"
    assert claims.main(["claim", target, "codex"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "claimed": True,
        "target": target,
        "agent": "codex",
    }
    assert claims.holder(target)["agent"] == "codex"
    assert claims.main(["release", target, "other"]) == 1
    assert json.loads(capsys.readouterr().out)["released"] is False
    assert claims.holder(target)["agent"] == "codex"
    assert claims.main(["release", target, "codex"]) == 0
    assert json.loads(capsys.readouterr().out)["released"] is True
    assert claims.holder(target) is None


def test_claims_list_and_reap(capsys, monkeypatch) -> None:
    monkeypatch.setattr(claims.time, "time", lambda: 10000.0)
    monkeypatch.setattr(claims, "_pid_alive", lambda pid: False)
    assert claims.claim("owner/repo#1", "codex", pid=123)
    assert claims.main([]) == 0
    assert json.loads(capsys.readouterr().out) == {"active_claims": {}}
    assert claims.main(["reap"]) == 0
    assert json.loads(capsys.readouterr().out) == {"reaped": []}
    monkeypatch.setattr(claims.time, "time", lambda: 10000.0 + claims.REAP_GRACE_SECONDS + 1)
    assert claims.main(["reap"]) == 0
    assert json.loads(capsys.readouterr().out) == {"reaped": ["owner/repo#1"]}
    assert not (claims._claims_dir() / claims._slug("owner/repo#1")).exists()
