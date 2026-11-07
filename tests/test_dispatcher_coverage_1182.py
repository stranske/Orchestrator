"""Ranked coverage targets for issue #1182 - dispatcher.py coverage.

This test file adds coverage for dispatcher.py functions identified by the
coverage-gap ranking in docs/verification/pr-710-dispatcher-planning/ranking.json.
Target: src/dispatcher.py (tier1_escaped_defects: 45.0, tier2_churn: 64, tier3_uncovered_statements: 0)
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest

import dispatcher


class TestDispatchBranch:
    """Tests for _dispatch_branch function."""

    def test_dispatch_branch_with_issue_number(self) -> None:
        """Test _dispatch_branch with a target containing an issue number."""
        # owner/repo is not in INFRA_REPOS, so it uses agent as prefix
        result = dispatcher._dispatch_branch("owner/repo#42", "opener", "codex", None)
        assert result == "codex/issue-42"

    def test_dispatch_branch_with_custom_prefix(self) -> None:
        """Test _dispatch_branch with a custom prefix."""
        result = dispatcher._dispatch_branch("owner/repo#42", "opener", "codex", "custom/")
        assert result == "custom/issue-42"

    def test_dispatch_branch_without_issue_number(self) -> None:
        """Test _dispatch_branch without an issue number."""
        # owner/repo is not in INFRA_REPOS, so it uses agent as prefix
        result = dispatcher._dispatch_branch("owner/repo", "opener", "codex", None)
        assert result == "codex/opener"

    def test_dispatch_branch_with_trailing_slash_in_prefix(self) -> None:
        """Test _dispatch_branch with trailing slash in prefix."""
        result = dispatcher._dispatch_branch("owner/repo#42", "opener", "codex", "custom//")
        assert result == "custom/issue-42"

    def test_dispatch_branch_invalid_prefix(self) -> None:
        """Test _dispatch_branch with invalid branch prefix."""
        with pytest.raises(ValueError, match="branch prefix must contain nonempty alphanumeric"):
            dispatcher._dispatch_branch("owner/repo#42", "opener", "codex", "../invalid")

    def test_dispatch_branch_infra_repo(self) -> None:
        """Test _dispatch_branch with an infra repo uses orch prefix."""
        # stranske/Workflows is in INFRA_REPOS, so it should use "orch" as prefix
        result = dispatcher._dispatch_branch("stranske/Workflows#42", "opener", "codex", None)
        assert result == "orch/issue-42"


class TestEnvInt:
    """Tests for _env_int function."""

    def test_env_int_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test _env_int with no environment variable set."""
        monkeypatch.delenv("TEST_ENV_INT", raising=False)
        result = dispatcher._env_int("TEST_ENV_INT", 42)
        assert result == 42

    def test_env_int_valid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test _env_int with a valid integer environment variable."""
        monkeypatch.setenv("TEST_ENV_INT", "123")
        result = dispatcher._env_int("TEST_ENV_INT", 42)
        assert result == 123

    def test_env_int_invalid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test _env_int with an invalid environment variable."""
        monkeypatch.setenv("TEST_ENV_INT", "not-a-number")
        result = dispatcher._env_int("TEST_ENV_INT", 42)
        assert result == 42


class TestEnvFloat:
    """Tests for _env_float function."""

    def test_env_float_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test _env_float with no environment variable set."""
        monkeypatch.delenv("TEST_ENV_FLOAT", raising=False)
        result = dispatcher._env_float("TEST_ENV_FLOAT", 3.14)
        assert result == 3.14

    def test_env_float_valid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test _env_float with a valid float environment variable."""
        monkeypatch.setenv("TEST_ENV_FLOAT", "2.71")
        result = dispatcher._env_float("TEST_ENV_FLOAT", 3.14)
        assert result == 2.71

    def test_env_float_invalid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test _env_float with an invalid environment variable."""
        monkeypatch.setenv("TEST_ENV_FLOAT", "not-a-float")
        result = dispatcher._env_float("TEST_ENV_FLOAT", 3.14)
        assert result == 3.14


class TestDispatchBranchEdgeCases:
    """Additional edge case tests for _dispatch_branch."""

    def test_dispatch_branch_agent_prefix(self) -> None:
        """Test _dispatch_branch uses agent as prefix when not infra repo."""
        result = dispatcher._dispatch_branch("owner/repo#99", "opener", "vibe", None)
        # Should use agent name as prefix
        assert result == "vibe/issue-99"

    def test_dispatch_branch_hyphenated_prefix(self) -> None:
        """Test _dispatch_branch with hyphenated prefix."""
        result = dispatcher._dispatch_branch("owner/repo#1", "opener", "codex", "my-prefix")
        assert result == "my-prefix/issue-1"

    def test_dispatch_branch_underscore_prefix(self) -> None:
        """Test _dispatch_branch with underscore in prefix."""
        result = dispatcher._dispatch_branch("owner/repo#1", "opener", "codex", "my_prefix")
        assert result == "my_prefix/issue-1"

    def test_dispatch_branch_mixed_chars_prefix(self) -> None:
        """Test _dispatch_branch with mixed valid characters in prefix."""
        result = dispatcher._dispatch_branch("owner/repo#1", "opener", "codex", "my-prefix_123")
        assert result == "my-prefix_123/issue-1"
