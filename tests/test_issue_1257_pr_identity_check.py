"""Test for issue #1257: pre-existing: `summary` on sync PR.

The summary check in pr-00-gate.yml was failing because it compared the resolved PR's
head SHA with the original event's head SHA, and for sync PRs that receive updates
between when the workflow is triggered and when the summary step runs, the head SHA
can legitimately differ.

The fix removes the head SHA comparison, keeping only the PR number and repository
checks which should always match.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest


class TestIssue1257PRIdentityCheck:
    """Reproduce and verify the fix for issue #1257."""

    def _old_check_logic(self, original, resolved):
        """The OLD logic that included head SHA in the check (fails for sync PRs)."""
        if original and (
            original.get("number") != resolved.get("number")
            or original.get("head", {}).get("sha") != resolved.get("head", {}).get("sha")
            or original.get("head", {}).get("repo", {}).get("full_name")
            != resolved.get("head", {}).get("repo", {}).get("full_name")
        ):
            raise RuntimeError("Resolved Gate PR identity differs from the original event")

    def _new_check_logic(self, original, resolved):
        """The NEW logic that excludes head SHA from the check (works for sync PRs)."""
        if original and (
            original.get("number") != resolved.get("number")
            or original.get("head", {}).get("repo", {}).get("full_name")
            != resolved.get("head", {}).get("repo", {}).get("full_name")
        ):
            raise RuntimeError("Resolved Gate PR identity differs from the original event")

    def test_old_logic_fails_on_head_sha_change(self):
        """The old logic raises RuntimeError when only the head SHA changes.

        This simulates a sync PR where the head SHA changed between when the workflow
        was triggered and when the summary step runs, but the PR number and repository
        are the same.
        """
        original = {
            "number": 1247,
            "head": {
                "sha": "abc123",
                "repo": {"full_name": "stranske/Orchestrator"},
            },
        }
        resolved = {
            "number": 1247,
            "head": {
                "sha": "def456",  # Different SHA (sync PR was updated)
                "repo": {"full_name": "stranske/Orchestrator"},
            },
        }

        with pytest.raises(RuntimeError, match="Resolved Gate PR identity differs"):
            self._old_check_logic(original, resolved)

    def test_new_logic_passes_on_head_sha_change(self):
        """The new logic does NOT raise when only the head SHA changes.

        This is the fix: for sync PRs, the head SHA can legitimately change, so
        we only check PR number and repository, not the head SHA.
        """
        original = {
            "number": 1247,
            "head": {
                "sha": "abc123",
                "repo": {"full_name": "stranske/Orchestrator"},
            },
        }
        resolved = {
            "number": 1247,
            "head": {
                "sha": "def456",  # Different SHA (sync PR was updated)
                "repo": {"full_name": "stranske/Orchestrator"},
            },
        }

        # Should not raise
        self._new_check_logic(original, resolved)

    def test_both_logic_fail_on_pr_number_change(self):
        """Both old and new logic fail when PR number changes (this is a real problem)."""
        original = {
            "number": 1247,
            "head": {
                "sha": "abc123",
                "repo": {"full_name": "stranske/Orchestrator"},
            },
        }
        resolved = {
            "number": 1248,  # Different PR number
            "head": {
                "sha": "abc123",
                "repo": {"full_name": "stranske/Orchestrator"},
            },
        }

        with pytest.raises(RuntimeError, match="Resolved Gate PR identity differs"):
            self._old_check_logic(original, resolved)

        with pytest.raises(RuntimeError, match="Resolved Gate PR identity differs"):
            self._new_check_logic(original, resolved)

    def test_both_logic_fail_on_repo_change(self):
        """Both old and new logic fail when repository changes (this is a real problem)."""
        original = {
            "number": 1247,
            "head": {
                "sha": "abc123",
                "repo": {"full_name": "stranske/Orchestrator"},
            },
        }
        resolved = {
            "number": 1247,
            "head": {
                "sha": "abc123",
                "repo": {"full_name": "stranske/OtherRepo"},  # Different repo
            },
        }

        with pytest.raises(RuntimeError, match="Resolved Gate PR identity differs"):
            self._old_check_logic(original, resolved)

        with pytest.raises(RuntimeError, match="Resolved Gate PR identity differs"):
            self._new_check_logic(original, resolved)

    def test_new_logic_passes_on_identical_pr(self):
        """The new logic passes when all fields are identical."""
        original = {
            "number": 1247,
            "head": {
                "sha": "abc123",
                "repo": {"full_name": "stranske/Orchestrator"},
            },
        }
        resolved = {
            "number": 1247,
            "head": {
                "sha": "abc123",
                "repo": {"full_name": "stranske/Orchestrator"},
            },
        }

        # Should not raise
        self._new_check_logic(original, resolved)

    def test_workflow_file_has_fixed_check(self):
        """Verify that pr-00-gate.yml has the fixed check (without head SHA comparison).

        This ensures the fix is actually in the workflow file.
        """
        gate_workflow = Path(__file__).parent.parent / ".github" / "workflows" / "pr-00-gate.yml"
        content = gate_workflow.read_text(encoding="utf-8")

        # The fix should have removed the head SHA check line
        # The old version had: or original.get("head", {}).get("sha") != resolved.get("head", {}).get("sha")
        # The new version should NOT have this line

        # Find the summarize step
        assert "summary:" in content, "Summary job not found in pr-00-gate.yml"

        # The check should NOT include head SHA comparison
        # Look for the pattern that would indicate head SHA is being checked
        lines = content.split("\n")
        found_check = False
        found_head_sha_check = False

        for i, line in enumerate(lines):
            if "Resolved Gate PR identity differs from the original event" in line:
                found_check = True
                # Look backwards for the condition
                for j in range(i - 1, max(0, i - 10), -1):
                    if "original.get(\"head\", {}).get(\"sha\")" in lines[j]:
                        found_head_sha_check = True
                        break

        # The check should exist but should NOT check head SHA
        assert found_check, "RuntimeError check not found in pr-00-gate.yml"
        assert not found_head_sha_check, (
            "Head SHA check still present in pr-00-gate.yml - the fix was not applied"
        )
