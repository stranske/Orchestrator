"""Hermetic coverage for admission, prerequisite, and ledger evidence boundaries."""

from __future__ import annotations

import json

import pytest

import capabilities
import capability_admission as admission
import env_prereq
import ledger_reconcile


@pytest.fixture(autouse=True)
def _hermetic_state(tmp_path, monkeypatch):
    """`admission.preflight` consults the capability ledger under a lock: keep that off the live one."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.setattr(capabilities, "REG", tmp_path / "capabilities.json")


def test_consumer_resolution_confines_paths_and_classifies_evidence(tmp_path):
    """Only real in-tree consumer symbols may satisfy the acting-consumer requirement."""
    modules = tmp_path / "src"
    repo = tmp_path / "repo"
    modules.mkdir()
    repo.mkdir()
    (modules / "consumer.py").write_text(
        "class Expected:\n    def consume(self):\n        return True\n\ndef consume():\n    return False\n"
    )
    (repo / "runner.sh").write_text("function run_it() { :; }\n")
    (tmp_path / "outside.py").write_text("def escape():\n    pass\n")

    cap = {
        "capability_id": "cap",
        "downstream_consumer": (
            "consumer.py:Expected.consume runner.sh:run_it src/../../outside.py:escape missing.py:nope"
        ),
        "event_history": [{"type": "invocation", "timestamp": 10}],
    }
    resolution = admission.consumer_resolution(cap, module_dir=modules, repo_root=repo)
    assert resolution["resolved"] == ["consumer.py:Expected.consume", "runner.sh:run_it"]
    assert resolution["unresolved"] == ["src/../../outside.py:escape", "missing.py:nope"]

    ctx = {"module_dir": modules, "repo_root": repo, "acted_on": {"cap": 2}}
    assert admission.consumer_class(cap, ctx)[0:2] == ("unresolved", False)
    cap["downstream_consumer"] = "consumer.py:Expected.consume"
    assert admission.consumer_class(cap, ctx)[0:2] == ("acting", True)
    cap["findability_category"] = admission.EXERCISE_CATEGORY
    assert admission.consumer_class(cap, ctx)[0:2] == ("exercise_bound", True)


def test_preflight_and_commitments_report_obligations_and_missing_records(tmp_path, monkeypatch):
    """Pre-build obligations and expired promises must remain visible rather than silently pass."""
    audits = tmp_path / "audits"
    audits.mkdir()
    root = tmp_path / "scan"
    root.mkdir()
    (root / "promise.md").write_text(
        "See 2026-01-01-missing.md\nORCH_TINY_UNTIL=2026-01-01\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(admission, "AUDITS", audits)
    monkeypatch.setattr(admission, "_today", lambda: "2026-02-01")
    result = admission.commitments(root=root)
    assert result["clean"] is False
    assert result["dangling_citations"][0]["record"] == "2026-01-01-missing.md"
    assert result["overdue_without_record"][0]["var"] == "ORCH_TINY_UNTIL"

    spec = {
        "capability_id": "proposed",
        "notes": "checked existing work; not present; building new",
        "downstream_consumer": "consumer.py:Expected.consume",
        "learning_sink": "brain",
        "kill_switch": "ORCH_STOP",
        "rollback": "revert",
        "expiry": "tomorrow",
        "findability_category": admission.EXERCISE_CATEGORY,
        "findability_rationale": "exercise it before registration",
    }
    preflight = admission.preflight(spec)
    assert {"caller_exists", "heartbeat", "fixture", "findable"} <= set(preflight["obligations"])
    assert preflight["checks"]["findable"]["ok"] is None


def test_env_prerequisites_name_the_specific_missing_state(tmp_path, monkeypatch):
    """The prerequisite helpers distinguish declared, historical, and live-state gaps."""
    rows = {
        "declared": {"capability_version_id": None, "status": "retired", "expiry": 1},
        "live": {"capability_version_id": "v1", "status": "live", "last_invocation": 3},
    }
    monkeypatch.setattr(env_prereq, "_ledger", lambda: rows)
    monkeypatch.setattr(capabilities, "declared_row_ids", lambda: {"pending"})
    declared = env_prereq.ledger_rows_absent("pending")
    historical = env_prereq.ledger_rows_absent("missing", "pending")
    assert env_prereq.DECLARED_UNREGISTERED_MARK in declared
    assert "missing" in historical and env_prereq.DECLARED_UNREGISTERED_MARK not in historical
    assert "no version lineage" in env_prereq.ledger_version_lineage_absent("declared")
    assert env_prereq.ledger_invocation_history_absent("declared")
    assert env_prereq.ledger_invocation_history_absent("live") is None
    monkeypatch.setattr(
        capabilities, "expiry_retirement", lambda row: "expired" if row["expiry"] else None
    )
    assert "retired by expiry" in env_prereq.ledger_rows_not_live("declared")

    core = tmp_path / "core"
    monkeypatch.setenv("CLOSER_CORE_SOURCE", str(core))
    assert env_prereq.CORE_SIBLING_ABSENT_MARK in env_prereq.core_sibling_absent()
    runtime = core / "scripts" / "closer_runtime.py"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("# present\n")
    assert env_prereq.core_sibling_absent() is None


def test_ledger_evidence_readers_ignore_other_runs_and_malformed_records(tmp_path):
    """Only a run's own valid evidence may contribute usage, cost, or resumability."""
    ledger = tmp_path / "ledger.ndjson"
    ledger.write_text('{"run_id":"a"}\nnot-json\n[]\n')
    rows, errors = ledger_reconcile._read_ledger(ledger)
    assert rows == [{"run_id": "a"}]
    assert len(errors) == 2

    run_id = "run-a"
    other = "run-b"
    log = tmp_path / "dispatch.log"
    log.write_text(
        "=== 2026-10-22T01:00:00Z run_id=run-a ===\n"
        + json.dumps({"usage": {"input_tokens": 3, "output_tokens": 5}, "cost_usd": 0.25})
        + '\nOWNER_QUESTION: {"question": "continue?", "default": "yes"}\n'
        + "session id: 12345678-1234-1234-1234-123456789abc\n"
        + "=== 2026-10-22T01:01:00Z run_id=run-b ===\n"
        + json.dumps({"usage": {"input_tokens": 99, "output_tokens": 99}, "cost_usd": 9})
    )
    writers = {run_id, other}
    assert ledger_reconcile._log_usage(log, run_id, writers) == (3, 5)
    assert ledger_reconcile._log_cost_usd(log, run_id, writers) == 0.25
    segment = ledger_reconcile._log_segment(log, run_id, writers)
    assert ledger_reconcile._resume_token_from_segment(segment) == (
        "codex_session",
        "12345678-1234-1234-1234-123456789abc",
    )
    assert ledger_reconcile._owner_questions_from_segment(segment) == [
        {"question": "continue?", "default": "yes"}
    ]
    assert ledger_reconcile._signal_death_rc({"rc": 143}) == 143
    assert ledger_reconcile._signal_death_rc({"rc": 999}) is None
