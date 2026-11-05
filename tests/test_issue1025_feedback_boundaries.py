"""Hermetic coverage for the next ranked #1025 source: feedback.py.

Main's Gate report (85.7%) still leaves feedback.py's operator CLI and two
classification branches unexecuted. These tests call those paths directly.
"""

from __future__ import annotations

import io
import json
import sys

import feedback


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    monkeypatch.setattr(feedback, "_capability_daily_heartbeat", lambda *_a, **_k: None)


def test_completion_events_text_mode_prints_each_episode(monkeypatch, tmp_path):
    """Without --json the CLI prints one episode per line, not one JSON array."""
    _isolate(monkeypatch, tmp_path)
    feedback.record_run("text-episode", "owner/repo#1025", "implement", "codex")
    feedback.record_completion_event("text-episode", phase="execution", producer="pytest")
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)

    assert feedback.main(["completion-events", "--limit", "5"]) == 0

    text = buf.getvalue()
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines, text
    assert not text.lstrip().startswith("[")
    episodes = [json.loads(line) for line in lines]
    assert all(episode["identity"]["run_id"] == "text-episode" for episode in episodes)


def test_questions_command_names_an_empty_queue(monkeypatch, tmp_path):
    """An empty owner queue must say so, rather than printing nothing."""
    _isolate(monkeypatch, tmp_path)
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)

    assert feedback.main(["questions"]) == 0

    assert "no open owner questions" in buf.getvalue()


def test_bare_feedback_main_reports_the_store_path(monkeypatch, tmp_path):
    """No subcommand still opens the store and names the path it used."""
    _isolate(monkeypatch, tmp_path)
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)

    assert feedback.main([]) == 0

    assert buf.getvalue().strip() == f"feedback store: {feedback.DB_PATH}"
    assert feedback.DB_PATH.is_file()


def test_role_operations_classify_as_role_backend():
    """A role operation is a role backend, including the names that do not use the prefix."""
    assert feedback.derive_operation_role("role_redirect") == "role_backend"
    assert feedback.derive_operation_role("decompose") == "role_backend"
    assert feedback.derive_operation_role("redirect_agent") == "role_backend"


def test_blank_artifact_fields_are_omitted():
    """An empty artifact field is absent, not stored as a blank hash."""
    redacted: list[str] = []
    clean = feedback._sanitize_artifact_refs(
        [{"artifact_id": "artifact-1", "kind": "diff", "content_hash": ""}],
        redacted,
    )
    assert clean == [{"artifact_id": "artifact-1", "kind": "diff"}]
    assert redacted == []
