"""Hermetic unit tests for capability_matcher_proposals formatting, DB scoring, and CLI modes."""

from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

import pytest

import capabilities
import capability_matcher_proposals as cmp
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
        raise AssertionError("capability_matcher_proposals tests must not spawn processes/sockets")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    yield runtime
    assert attempted == [], f"unexpected external calls: {attempted}"


def _cap(cap_id: str, **overrides) -> dict:
    row = capabilities._blank_capability(cap_id)
    row["status"] = "generated"
    row.update(overrides)
    return row


def test_task_counts_reads_runs_from_isolated_feedback_store() -> None:
    feedback.record_run("run:cm:1", "o/r#1", "codemod", "codex")
    feedback.record_run("run:cm:2", "o/r#2", "codemod", "claude")
    feedback.record_run("run:rev:1", "o/r#3", "review", "gemini")

    assert cmp._task_counts() == {"codemod": 2, "review": 1}

    conn = feedback._conn()
    try:
        assert cmp._task_counts(conn=conn) == {"codemod": 2, "review": 1}
        assert conn.execute("SELECT 1").fetchone() == (1,)
    finally:
        conn.close()


def test_format_report_renders_should_have_bullets_and_all_row_states(
    isolated_runtime: Path,
) -> None:
    ledger = isolated_runtime / "capabilities.json"
    capabilities.save(
        {
            "codemod-campaign": _cap("codemod-campaign"),
            "adversarial-review": _cap("adversarial-review", last_invocation=1700000000),
            "feedback-store": _cap("feedback-store"),
        },
        ledger,
    )
    rep = cmp.evaluate(path=ledger, task_counts={"codemod": 5, "review": 9})
    rendered = cmp.format_report(rep)

    work = sum(1 for p in cmp.PROPOSALS.values() if p["kind"] == cmp.WORK_ROUTED)
    infra = sum(1 for p in cmp.PROPOSALS.values() if p["kind"] == cmp.INFRASTRUCTURE)
    assert f"{work} work-routed · {infra} infrastructure" in rendered
    assert (
        "## SHOULD HAVE BEEN USED: 1 (5 runs of matching work ran while the capability never did)"
        in rendered
    )
    assert "- **codemod-campaign** — 5 matching runs." in rendered
    assert "- **adversarial-review**" not in rendered
    assert "| codemod-campaign | work_routed | 5 | no | YES |" in rendered
    assert "| adversarial-review | work_routed | 9 | yes | no |" in rendered
    assert "| feedback-store | infrastructure | n/a | no | — |" in rendered


def test_main_supports_markdown_json_and_apply_modes_on_isolated_ledger(
    isolated_runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = isolated_runtime / "capabilities.json"
    capabilities.save(
        {
            "codemod-campaign": _cap("codemod-campaign"),
            "testgen-lane": _cap(
                "testgen-lane",
                matcher={"field": "task_type", "operator": "in", "value": ["custom_preexisting"]},
            ),
            "feedback-store": _cap("feedback-store"),
        },
        ledger,
    )
    feedback.record_run("run:cm:1", "o/r#1", "codemod", "codex")
    feedback.record_run("run:cm:2", "o/r#2", "codemod", "codex")
    before_bytes = ledger.read_bytes()

    assert cmp.main([]) == 0
    text_out = capsys.readouterr().out
    assert "## SHOULD HAVE BEEN USED: 1 (2 runs" in text_out
    assert ledger.read_bytes() == before_bytes

    assert cmp.main(["--json"]) == 0
    json_out = json.loads(capsys.readouterr().out)
    assert json_out["should_have_been_used"] == ["codemod-campaign"]
    assert json_out["missed_run_total"] == 2
    assert ledger.read_bytes() == before_bytes

    assert cmp.main(["--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied == {
        "written": ["codemod-campaign"],
        "skipped": [cid for cid in sorted(cmp.PROPOSALS) if cid != "codemod-campaign"],
        "dry_run": False,
    }
    saved = capabilities.load_declared(ledger)
    assert saved["codemod-campaign"]["matcher"] == cmp._m("codemod")
    assert saved["testgen-lane"]["matcher"]["value"] == ["custom_preexisting"]
    assert saved["feedback-store"]["matcher"] is None

    assert cmp.main(["--apply", "--include-infrastructure"]) == 0
    applied_infra = json.loads(capsys.readouterr().out)
    assert applied_infra["written"] == ["feedback-store"]
    assert capabilities.load_declared(ledger)["feedback-store"]["matcher"] == {
        "kind": "feedback_event",
        "name": "record_run",
    }
