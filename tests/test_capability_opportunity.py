"""Hermetic unit tests for capability_opportunity trigger replay, DB counts, and CLI reporting."""

from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

import pytest

import capabilities
import capability_opportunity as co
import feedback


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    state.mkdir()
    runtime.mkdir()
    ledger = runtime / "capabilities.json"
    db_path = runtime / "feedback.db"

    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(runtime))
    monkeypatch.setenv("ORCH_CAPABILITIES_PATH", str(ledger))
    monkeypatch.setenv("ORCH_FEEDBACK_DB", str(db_path))

    monkeypatch.setattr(capabilities, "REG", ledger)
    monkeypatch.setattr(capabilities.load, "__defaults__", (ledger,))
    monkeypatch.setattr(capabilities.load_declared, "__defaults__", (ledger,))
    monkeypatch.setattr(capabilities, "FEATURES_REG", runtime / "features.json")
    monkeypatch.setattr(capabilities, "KNOWN_GATES", {})
    monkeypatch.setattr(capabilities, "KNOWN_DECLARATIONS", {})

    monkeypatch.setattr(feedback, "DB_PATH", db_path)
    monkeypatch.setattr(feedback, "_capability_daily_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(capabilities, "production_heartbeat", lambda *a, **k: None)

    attempted: list[tuple] = []

    def forbidden(*args, **kwargs):
        attempted.append((args, kwargs))
        raise AssertionError("capability_opportunity tests must not spawn processes or sockets")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    yield runtime
    assert attempted == [], f"unexpected external calls: {attempted}"


def _cap(cap_id: str, **overrides) -> dict:
    row = capabilities._blank_capability(cap_id)
    row["status"] = "wired"
    row.update(overrides)
    return row


def test_classify_trigger_preserves_custom_internal_matcher_dict() -> None:
    custom = _cap("custom-cap", matcher={"custom_signal": "tick_end", "window": 5})
    trig = co.classify_trigger(custom)
    assert trig == {
        "kind": co.TRIGGER_INTERNAL,
        "detail": {"custom_signal": "tick_end", "window": 5},
    }

    assessed = co.assess("custom-cap", custom, task_counts={"implement": 4}, role_counts={}, env={})
    assert assessed["trigger_kind"] == co.TRIGGER_INTERNAL
    assert assessed["opportunities"] is None
    assert assessed["verdict"] == co.VERDICT_UNKNOWN
    assert "internal trigger (custom)" in assessed["note"]


def test_assess_marks_capability_with_outcome_links_as_in_use() -> None:
    linked = _cap(
        "linked-cap",
        matcher={"custom_signal": "bridge_event"},
        outcome_links=["effect:1", "effect:2"],
    )
    unlinked = _cap("linked-cap", matcher={"custom_signal": "bridge_event"}, outcome_links=[])
    assessed_linked = co.assess("linked-cap", linked, task_counts={}, role_counts={}, env={})
    assessed_unlinked = co.assess("linked-cap", unlinked, task_counts={}, role_counts={}, env={})
    assert assessed_linked["outcome_links"] == 2
    assert assessed_linked["verdict"] == co.VERDICT_IN_USE
    assert assessed_unlinked["verdict"] == co.VERDICT_UNKNOWN

    # Env-gated OFF must stay DELIBERATELY_OFF even if historical outcome_links exist.
    gated = _cap(
        "gated-cap",
        matcher={"kind": "env", "name": "ORCH_GATE_TEST", "equals": "1"},
        outcome_links=["effect:old"],
    )
    assert (
        co.assess("gated-cap", gated, task_counts={}, role_counts={}, env={})["verdict"]
        == co.VERDICT_DELIBERATELY_OFF
    )


def test_task_and_role_counts_read_isolated_feedback_db_and_close_owned_conn() -> None:
    feedback.record_run("run:impl:1", "o/r#1", "implement", "codex")
    feedback.record_run("run:impl:2", "o/r#2", "implement", "claude")
    feedback.record_run("run:docs:1", "o/r#3", "docs", "gemini")
    feedback.record_role_run("role:triage:gemini:1", "triage", "o/r#4", "gemini")
    feedback.record_role_run("role:triage:gemini:2", "triage", "o/r#5", "gemini")
    feedback.record_role_run("role:redirect:codex:1", "redirect", "o/r#6", "codex")

    assert co._task_type_counts() == {
        "implement": 2,
        "docs": 1,
        "role:triage": 2,
        "role:redirect": 1,
    }
    assert co._role_invocation_counts() == {"triage": 2, "redirect": 1}

    conn = feedback._conn()
    try:
        assert co._task_type_counts(conn=conn)["implement"] == 2
        assert co._role_invocation_counts(conn=conn) == {"triage": 2, "redirect": 1}
        assert conn.execute("SELECT 1").fetchone() == (1,)
    finally:
        conn.close()


def test_main_and_report_emit_json_and_markdown_without_writing_ledger(
    isolated_runtime: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ORCH_FEATURE_OFF", raising=False)
    ledger = isolated_runtime / "capabilities.json"
    capabilities.save(
        {
            "cap-never": _cap(
                "cap-never",
                matcher={"field": "task_type", "operator": "in", "value": ["implement"]},
            ),
            "cap-off": _cap(
                "cap-off",
                matcher={"kind": "env", "name": "ORCH_FEATURE_OFF", "equals": "1"},
            ),
            "cap-used": _cap(
                "cap-used",
                matcher={"kind": "role", "equals": "triage"},
                last_invocation=1700000000,
            ),
            "cap-idle": _cap(
                "cap-idle",
                matcher={"field": "task_type", "operator": "in", "value": ["epic"]},
            ),
            "cap-none": _cap("cap-none", matcher=None),
        },
        ledger,
    )
    feedback.record_run("run:impl:1", "o/r#1", "implement", "codex")
    feedback.record_role_run("role:triage:gemini:1", "triage", "o/r#2", "gemini")
    before_bytes = ledger.read_bytes()

    assert co.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 5
    assert payload["total_runs_considered"] == 2
    assert payload["by_verdict"] == {
        co.VERDICT_DELIBERATELY_OFF: ["cap-off"],
        co.VERDICT_IN_USE: ["cap-used"],
        co.VERDICT_NEVER_CONSIDERED: ["cap-never"],
        co.VERDICT_NO_MATCHING_WORK: ["cap-idle"],
        co.VERDICT_UNKNOWN: ["cap-none"],
    }

    assert co.main([]) == 0
    text = capsys.readouterr().out
    assert "5 capabilities assessed against 2 recorded runs." in text
    assert "| cap-never | task_type | 1 | no | 0 | never_considered |" in text
    assert "| cap-used | role | 1 | yes | 0 | in_use |" in text
    assert ledger.read_bytes() == before_bytes
