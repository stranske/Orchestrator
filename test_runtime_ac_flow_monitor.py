from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import feedback
import runtime_ac
import runtime_ac_flow_monitor as monitor
import runtime_ac_gate


def _new_monitor_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    monitor._create_schema(conn)
    return conn


def test_ineligible_closers_do_not_trigger_zero_flow(tmp_path: Path) -> None:
    now = 2_000_000
    db_path = tmp_path / "orchestrator.db"
    conn = _new_monitor_db(db_path)
    try:
        for index in range(100):
            target = f"owner/repo#{index + 1}"
            conn.execute(
                "INSERT INTO runs(run_id,ts,target,task_type,agent,mode,source) VALUES(?,?,?,?,?,?,?)",
                (f"closer-{index}", now - index, target, "implement", "codex", "remote", "keepalive"),
            )
            monitor._insert_gate_event(
                conn,
                event_id=f"ineligible-{index}",
                now=now - index,
                target=target,
                required=False,
                status="skipped",
            )
        conn.commit()
    finally:
        conn.close()

    report = monitor.build_report(
        db_path, tmp_path / "missing.log", 72, 1, 5, now_ts=now
    )
    assert report["closer_proxy_present"] is True
    assert report["zero_flow_alert"] is False, (
        "ineligible closer traffic triggered zero-flow alert"
    )
    assert report["required_event_denominator"] == 0


def test_required_gate_that_does_not_execute_triggers_zero_flow(tmp_path: Path) -> None:
    now = 2_000_000
    db_path = tmp_path / "orchestrator.db"
    conn = _new_monitor_db(db_path)
    try:
        monitor._insert_gate_event(
            conn,
            event_id="required-missing",
            now=now,
            target="owner/repo#500",
            required=True,
            status="missing_spec",
        )
        conn.commit()
    finally:
        conn.close()
    report = monitor.build_report(
        db_path, tmp_path / "missing.log", 72, 1, 5, now_ts=now
    )
    assert report["required_event_denominator"] == 1
    assert report["executed_gate_numerator"] == 0
    assert report["zero_flow_alert"] is True


def _target_spec(target: str) -> dict:
    spec = json.loads(json.dumps(runtime_ac._valid_spec()))
    spec["verification"]["target"] = target
    spec["verification"]["repo"] = target.split("#", 1)[0]
    return spec


def _fake_gate(verdict: str):
    def run(spec, **kwargs):
        return {
            "gate": {
                "verification_id": f"fixture-{verdict.lower()}",
                "verdict": verdict,
                "verifier_verdict": f"{verdict}_RUNTIME_AC",
                "pass_ratio": 1.0 if verdict == "PASS" else 0.0,
                "result_count": 1,
                "blocking": [] if verdict != "FAIL" else [{"check_id": "AC1"}],
                "needs_review": [] if verdict != "NEEDS_REVIEW" else [{"check_id": "AC1"}],
            }
        }

    return run


def test_gate_records_missing_and_each_executed_verdict(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    spec_dir = tmp_path / "specs"
    missing_item = {
        "target": "owner/repo#1",
        "lane": "closer",
        "task_type": "implement",
        "labels": ["runtime-ac"],
    }
    missing = runtime_ac_gate.gate_status(
        missing_item,
        dry_run=False,
        env={"ORCH_RUN_RUNTIME_AC": "1"},
        spec_dir=spec_dir,
        latest_run_fn=lambda target, mode=None: None,
    )
    assert missing["status"] == "missing_spec" and missing["blocks"] is True

    for index, verdict in enumerate(("PASS", "NEEDS_REVIEW", "FAIL"), start=2):
        target = f"owner/repo#{index}"
        installed = runtime_ac_gate.materialize_range_spec(
            target, _target_spec(target), spec_dir=spec_dir
        )
        assert installed["status"] == "materialized", installed
        result = runtime_ac_gate.gate_status(
            {**missing_item, "target": target, "labels": []},
            dry_run=False,
            env={"ORCH_RUN_RUNTIME_AC": "1"},
            spec_dir=spec_dir,
            run_fn=_fake_gate(verdict),
            latest_run_fn=lambda target, mode=None: None,
        )
        assert result["verdict"] == verdict
        assert result["blocks"] is (verdict != "PASS")

    events = feedback.runtime_ac_gate_events(limit=100)
    assert any(row["gate_status"] == "missing_spec" for row in events)
    assert {row.get("verifier_verdict") for row in events} >= {
        "PASS", "NEEDS_REVIEW", "FAIL"
    }


def test_range_materialization_is_exact_target_and_hash(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    spec_dir = tmp_path / "specs"
    workflows_spec = _target_spec("stranske/Workflows#2742")
    rejected = runtime_ac_gate.materialize_range_spec(
        "stranske/Pension-Data#703", workflows_spec, spec_dir=spec_dir
    )
    assert rejected["status"] == "materialization_failed"
    assert "spec_target_mismatch" in rejected["terminal_reason"]
    assert not runtime_ac_gate.spec_path(
        "stranske/Pension-Data#703", spec_dir=spec_dir
    ).exists(), "Workflows spec attached to Pension target"

    target = "stranske/Pension-Data#703"
    installed = runtime_ac_gate.materialize_range_spec(
        target, _target_spec(target), spec_dir=spec_dir, producer_run_id="range-703"
    )
    next_gate = runtime_ac_gate.gate_status(
        {"target": target, "lane": "closer", "task_type": "implement", "labels": []},
        dry_run=False,
        env={"ORCH_RUN_RUNTIME_AC": "1"},
        spec_dir=spec_dir,
        run_fn=_fake_gate("PASS"),
        latest_run_fn=lambda target, mode=None: None,
    )
    assert next_gate["spec_path"] == installed["spec_path"]
    assert next_gate["spec_hash"] == installed["spec_hash"]


def test_gate_event_joins_closer_verifier_and_downstream_outcome(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    target = "owner/repo#42"
    run_id = "remote-run-42"
    feedback.record_run(run_id, target, "implement", "codex", mode="remote")
    installed = runtime_ac_gate.materialize_range_spec(
        target, _target_spec(target), spec_dir=tmp_path / "specs"
    )
    assert installed["status"] == "materialized"

    def record_outcome(run_id_arg, gate):
        feedback.record_outcome(
            run_id_arg,
            verifier_verdict=gate["verdict"],
            adjudicated_verdict=gate["verdict"],
            merged=True,
            durability="durable",
        )
        return {"recorded": True}

    gate = runtime_ac_gate.gate_status(
        {"target": target, "lane": "closer", "task_type": "implement", "labels": []},
        dry_run=False,
        env={"ORCH_RUN_RUNTIME_AC": "1"},
        spec_dir=tmp_path / "specs",
        run_fn=_fake_gate("PASS"),
        latest_run_fn=lambda target, mode=None: run_id,
        record_fn=record_outcome,
    )
    assert gate["closer_run_id"] == run_id
    assert gate["verifier_run_id"] == "fixture-pass"
    event = next(
        row for row in feedback.runtime_ac_gate_events(limit=20)
        if row.get("gate_status") == "executed"
    )
    assert event["closer_run_id"] == run_id
    assert event["verifier_run_id"] == "fixture-pass"
    assert event["downstream_verdict"] == "PASS"
    assert event["downstream_merged"] is True
    assert event["downstream_durability"] == "durable"
