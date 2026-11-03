"""Hermetic regression coverage for coverage-autopilot round 880."""

from __future__ import annotations

import json
import subprocess

import pytest

import capability_activation_audit as audit
import exp_abcd
import local_verify


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    """Keep coverage probes from touching the shared runtime or launching anything."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.setattr(audit, "STATE_DIR", tmp_path / "state")
    attempted = []

    def forbid_external(*args, **kwargs):
        attempted.append((args, kwargs))
        pytest.fail("coverage test attempted an external process or network connection")

    monkeypatch.setattr(subprocess, "run", forbid_external)
    monkeypatch.setattr(subprocess, "Popen", forbid_external)
    yield tmp_path
    assert attempted == []


def test_local_verify_probe_environment_removes_only_pytest_addopts(monkeypatch):
    monkeypatch.setenv("PYTEST_ADDOPTS", "-x -n auto")
    monkeypatch.setenv("ROUND_880_SENTINEL", "kept")
    env = local_verify._probe_env()
    assert env["PYTEST_ADDOPTS"] == ""
    assert env["ROUND_880_SENTINEL"] == "kept"


def test_audit_capability_records_task_direct_and_gated_failures(monkeypatch):
    index = {"repos": {"o/r": ["task:missing"]}, "read_at": {"o/r": 1}, "unread": {}}
    task = audit.audit_capability(
        "task",
        {"status": "active", "matcher": {"field": "task_type", "value": ["missing"]}},
        emittable=set(),
        templates=set(),
        label_index=index,
        vocab_gaps={"task": ["task:typo"]},
        reach={"reachable": [], "by_capability": {}, "regressed": ["task"]},
    )
    assert task["reachable"] is False
    assert set(task["defects"]) == {
        "advisor_reach_regression",
        "no_prompt_template",
        "task_type_not_emittable",
        "vocabulary_mismatch",
    }

    monkeypatch.setattr(
        audit, "heartbeat_reachable", lambda _cap: {"status": "off_path", "detail": "x"}
    )
    monkeypatch.setattr(audit, "_entrypoint_files", lambda _cap: [audit.HERE / "driver.py"])
    direct = audit.audit_capability(
        "direct",
        {"status": "active", "matcher": {"kind": "transport"}, "entrypoint": "driver.py"},
        emittable=set(),
        templates=set(),
        label_index=index,
        env_gate={"suppressed_modules": ["driver.py"]},
    )
    assert direct["defects"] == ["heartbeat_env_suppressed", "heartbeat_off_path"]
    assert direct["heartbeat_env_suppressed"] == ["driver.py"]

    monkeypatch.setattr(audit.capabilities, "gate_policy", lambda _cap: {"encoded": False})
    gated = audit.audit_capability(
        "gated",
        {
            "status": "active",
            "matcher": {"kind": "env", "name": "ORCH_GATE"},
            "gate_reason": "manual",
        },
        emittable=set(),
        templates=set(),
        label_index=index,
    )
    assert gated["gate_flag"] == "ORCH_GATE"
    assert "machine-checkable" in gated["notes"][-1]


def test_audit_aggregates_live_rows_and_progress_history(monkeypatch, isolated_runtime):
    monkeypatch.setattr(audit, "_capability_heartbeat", lambda: None)
    monkeypatch.setattr(audit, "emittable_task_types", lambda: {"ok"})
    monkeypatch.setattr(audit, "_prompt_templates", lambda: {"ok"})
    monkeypatch.setattr(
        audit, "_fleet_label_index", lambda **_kw: {"repos": {}, "read_at": {}, "unread": {}}
    )
    monkeypatch.setattr(audit, "vocabulary_gaps", lambda _index: {})
    monkeypatch.setattr(audit, "heartbeat_env_gate", lambda: {"suppressed_modules": []})
    monkeypatch.setattr(
        audit,
        "advisor_reach",
        lambda _caps: {"reachable": [], "by_capability": {}, "regressed": []},
    )
    monkeypatch.setattr(audit.capabilities, "NOT_LIVE_STATES", {"retired"})
    ledger = {
        "live": {"status": "active", "matcher": {"field": "task_type", "value": ["ok"]}},
        "old": {"status": "retired", "matcher": {"kind": "transport"}},
    }
    report = audit.audit(ledger=ledger)
    assert (report["total"], report["reachable"], report["not_audited"]) == (
        1,
        1,
        {"retired": ["old"]},
    )
    history = isolated_runtime / "state" / "history.json"
    assert audit.record_snapshot(report, path=history) == {"recorded": True, "snapshots": 1}
    moved = dict(report, generated_at=report["generated_at"] + 1, reachable_ids=[])
    moved["by_defect"] = {"no_heartbeat": ["live"]}
    moved["not_audited"] = {"retired": ["live"]}
    change = audit.progress(moved, path=history)
    assert change["retired_since"] == {"retired": ["live"]}
    assert change["regressed"] == []


def test_experiment_identity_parsing_panel_and_cli(monkeypatch, capsys):
    assert exp_abcd.member_identity("arm one", "codex", 0) == "arm-one--member-01-codex"
    assert (
        exp_abcd.exp_diff_path("codex", "arm--member-01-codex") == "diff-arm--member-01-codex.patch"
    )
    with pytest.raises(ValueError, match="duplicate"):
        exp_abcd.experiment_members(
            {"members": [{"arm_id": "a", "member_id": "same", "agent": "codex"}] * 2}
        )
    assert exp_abcd.experiment_members({"agents": ["codex"]})[0]["legacy"] is True
    assert exp_abcd._extract_json('x {"scores": {"A": 1}} y') == {"scores": {"A": 1}}
    assert exp_abcd._extract_evidence_gaps(
        {"evidence_gaps": [" Need logs ", "need LOGS", {"gap": "trace"}]}
    ) == ["Need logs", "trace"]
    assert exp_abcd._with_probation(
        ["codex", "cursor"],
        protected={"codex"},
        rng=type("R", (), {"choice": lambda *_: "muse"})(),
        unproven=["muse"],
    ) == ["codex", "muse"]
    with pytest.raises(ValueError, match="empty"):
        exp_abcd._normalize_evaluator_specs([], default_agents=["codex"], implementer_agents=[])

    monkeypatch.setattr(exp_abcd, "status", lambda exp_id: {"id": exp_id})
    assert exp_abcd.main(["status", "demo"]) == 0
    assert json.loads(capsys.readouterr().out) == {"id": "demo"}
    assert exp_abcd.main(["unknown"]) == 2
