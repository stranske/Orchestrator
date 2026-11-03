"""Hermetic coverage for feedback.py — first ranked #1025 targets.

Priority order (escaped-defect, churn, uncovered mass):
  2. feedback.py — 40.0 escaped defect, 69 churn, 3702 uncovered

Skip capability_advisor.py and capabilities.py (already covered by PR #1027).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import feedback


class TestWorkItemSubject:
    """work_item_subject() — validate work item target format."""

    def test_returns_target_for_valid_work_item(self):
        """Returns the target when it matches owner/repo#N format."""
        result = feedback.work_item_subject("owner/repo#123")
        assert result == "owner/repo#123"

    def test_returns_none_for_placeholder(self):
        """Returns None for placeholder targets."""
        result = feedback.work_item_subject("redirect-role")
        assert result is None

    def test_returns_none_for_batch(self):
        """Returns None for batch targets."""
        result = feedback.work_item_subject("triage:9-items")
        assert result is None

    def test_returns_none_for_new_issue(self):
        """Returns None for #new."""
        result = feedback.work_item_subject("#new")
        assert result is None

    def test_returns_none_for_repo_only(self):
        """Returns None for owner/repo without issue number."""
        result = feedback.work_item_subject("owner/repo")
        assert result is None

    def test_returns_target_for_valid_with_underscores(self):
        """Returns the target when it has underscores in repo name."""
        result = feedback.work_item_subject("owner/my_repo#456")
        assert result == "owner/my_repo#456"

    def test_strips_whitespace(self):
        """Strips whitespace from target."""
        result = feedback.work_item_subject("  owner/repo#123  ")
        assert result == "owner/repo#123"

    def test_returns_none_for_none_input(self):
        """Returns None for None input."""
        result = feedback.work_item_subject(None)
        assert result is None

    def test_returns_none_for_empty_string(self):
        """Returns None for empty string."""
        result = feedback.work_item_subject("")
        assert result is None


class TestResolveDbPath:
    """resolve_db_path() — resolve Brain database path."""

    def test_returns_explicit_path(self, tmp_path):
        """Explicit path takes precedence."""
        explicit = tmp_path / "explicit.db"
        result = feedback.resolve_db_path(explicit=explicit)
        assert result == explicit.resolve()

    def test_returns_state_dir_path(self, tmp_path, monkeypatch):
        """State directory is used when no env vars are set."""
        state_dir = tmp_path / "state"
        expected = state_dir / "feedback" / "orchestrator.db"
        result = feedback.resolve_db_path(state_dir=state_dir)
        assert result == expected.resolve()

    def test_returns_orch_state_dir_path(self, tmp_path, monkeypatch):
        """ORCH_STATE_DIR is used when set."""
        orch_state = tmp_path / "orch_state"
        monkeypatch.setenv("ORCH_STATE_DIR", str(orch_state))
        expected = orch_state / "feedback" / "orchestrator.db"
        result = feedback.resolve_db_path()
        assert result == expected.resolve()

    def test_returns_orch_local_runtime_path(self, tmp_path, monkeypatch):
        """ORCH_LOCAL_RUNTIME is used when set."""
        local_runtime = tmp_path / "local_runtime"
        monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(local_runtime))
        monkeypatch.delenv("ORCH_STATE_DIR", raising=False)
        expected = local_runtime / "feedback" / "orchestrator.db"
        result = feedback.resolve_db_path()
        assert result == expected.resolve()

    def test_returns_explicit_path_takes_precedence(self, tmp_path, monkeypatch):
        """Explicit path takes precedence over state_dir."""
        explicit = tmp_path / "explicit.db"
        state_dir = tmp_path / "state"
        result = feedback.resolve_db_path(explicit=explicit, state_dir=state_dir)
        assert result == explicit.resolve()

    def test_expands_user_home(self, tmp_path, monkeypatch):
        """Path with ~ is expanded."""
        monkeypatch.delenv("ORCH_STATE_DIR", raising=False)
        monkeypatch.delenv("ORCH_LOCAL_RUNTIME", raising=False)
        monkeypatch.delenv("ORCH_FEEDBACK_DB", raising=False)
        state_dir = tmp_path / "state"
        result = feedback.resolve_db_path(state_dir=state_dir)
        assert result.is_absolute()
        assert "~" not in str(result)
