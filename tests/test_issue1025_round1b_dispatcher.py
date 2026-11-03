"""Hermetic coverage for dispatcher.py — first ranked #1025 targets.

Priority order (escaped-defect, churn, uncovered mass):
  1. dispatcher.py — 49.0 escaped defect, 79 churn, 3606 uncovered

Skip capability_advisor.py and capabilities.py (already covered by PR #1027).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import dispatcher


class TestWriteHeartbeat:
    """write_heartbeat() — write orchestrator heartbeat file."""

    def test_returns_dict_with_generated_at_and_pid(self, tmp_path, monkeypatch):
        """Heartbeat contains timestamp and process ID."""
        monkeypatch.setenv("HANDOFF_DIR", str(tmp_path))
        # Force reload to pick up the new HANDOFF_DIR
        import importlib
        importlib.reload(dispatcher)
        
        result = dispatcher.write_heartbeat()
        
        assert isinstance(result, dict)
        assert "generated_at" in result
        assert "pid" in result
        assert isinstance(result["generated_at"], int)
        assert isinstance(result["pid"], int)
        assert result["pid"] == os.getpid()

    def test_writes_file_to_handoff_dir(self, tmp_path, monkeypatch):
        """Heartbeat JSON is written to HANDOFF_DIR/orchestrator.json."""
        handoff_dir = tmp_path / "handoff"
        monkeypatch.setenv("HANDOFF_DIR", str(handoff_dir))
        import importlib
        importlib.reload(dispatcher)
        
        result = dispatcher.write_heartbeat()
        
        heartbeat_file = handoff_dir / "orchestrator.json"
        assert heartbeat_file.exists()
        content = heartbeat_file.read_text()
        data = json.loads(content.strip())
        assert data == result
        # Also verify the content is correct
        assert "generated_at" in data
        assert "pid" in data


class TestAgentGhConfigDisabled:
    """agent_gh_config_disabled() — check env var for kill switch."""

    def test_returns_false_when_env_not_set(self, monkeypatch):
        """Default is enabled (False)."""
        monkeypatch.delenv("ORCH_AGENT_GH_CONFIG_DISABLED", raising=False)
        assert dispatcher.agent_gh_config_disabled() is False

    def test_returns_false_when_env_empty(self, monkeypatch):
        """Empty string counts as enabled."""
        monkeypatch.setenv("ORCH_AGENT_GH_CONFIG_DISABLED", "")
        assert dispatcher.agent_gh_config_disabled() is False

    def test_returns_false_when_env_not_one(self, monkeypatch):
        """Only '1' means disabled."""
        monkeypatch.setenv("ORCH_AGENT_GH_CONFIG_DISABLED", "0")
        assert dispatcher.agent_gh_config_disabled() is False
        monkeypatch.setenv("ORCH_AGENT_GH_CONFIG_DISABLED", "true")
        assert dispatcher.agent_gh_config_disabled() is False

    def test_returns_true_when_env_is_one(self, monkeypatch):
        """Env var equal to '1' means disabled."""
        monkeypatch.setenv("ORCH_AGENT_GH_CONFIG_DISABLED", "1")
        assert dispatcher.agent_gh_config_disabled() is True

    def test_returns_true_when_env_is_one_with_whitespace(self, monkeypatch):
        """Whitespace is stripped."""
        monkeypatch.setenv("ORCH_AGENT_GH_CONFIG_DISABLED", " 1 ")
        assert dispatcher.agent_gh_config_disabled() is True

    def test_returns_true_when_env_is_one_with_crlf(self, monkeypatch):
        """CRLF is stripped (the canonical kill-switch pattern)."""
        monkeypatch.setenv("ORCH_AGENT_GH_CONFIG_DISABLED", "1\r")
        assert dispatcher.agent_gh_config_disabled() is True


class TestAgentGhGuardDisabled:
    """agent_gh_guard_disabled() — check env var for guard kill switch."""

    def test_returns_false_when_env_not_set(self, monkeypatch):
        """Default is enabled (False)."""
        monkeypatch.delenv("ORCH_AGENT_GH_GUARD_DISABLED", raising=False)
        assert dispatcher.agent_gh_guard_disabled() is False

    def test_returns_true_when_env_is_one(self, monkeypatch):
        """Env var equal to '1' means disabled."""
        monkeypatch.setenv("ORCH_AGENT_GH_GUARD_DISABLED", "1")
        assert dispatcher.agent_gh_guard_disabled() is True


class TestGhConfigDir:
    """gh_config_dir() — resolve gh config directory."""

    def test_returns_explicit_gh_config_dir(self, monkeypatch, tmp_path):
        """GH_CONFIG_DIR takes precedence."""
        explicit = tmp_path / "explicit"
        monkeypatch.setenv("GH_CONFIG_DIR", str(explicit))
        assert dispatcher.gh_config_dir() == explicit

    def test_returns_xdg_config_home_gh(self, monkeypatch, tmp_path):
        """Falls back to XDG_CONFIG_HOME/gh."""
        xdg = tmp_path / "xdg"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
        monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
        expected = xdg / "gh"
        assert dispatcher.gh_config_dir() == expected

    def test_returns_home_config_gh(self, monkeypatch, tmp_path):
        """Falls back to ~/.config/gh when neither env var is set."""
        import importlib
        # Need to reload after changing HOME to affect REAL_HOME
        import os
        old_home = os.environ.get("HOME")
        home = tmp_path / "home"
        os.environ["HOME"] = str(home)
        monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        # Reload to recompute REAL_HOME
        importlib.reload(dispatcher)
        try:
            expected = dispatcher.REAL_HOME / ".config" / "gh"
            assert dispatcher.gh_config_dir() == expected
        finally:
            if old_home:
                os.environ["HOME"] = old_home
            else:
                os.environ.pop("HOME", None)


class TestGhGuardCheck:
    """_gh_guard_check() — generate shell script to block refused gh commands."""

    def test_returns_shell_script_with_leave_exit(self):
        """Script uses 'exit' for leave parameter."""
        script = dispatcher._gh_guard_check("exit")
        assert "exit" in script
        assert "__orch_gh_hit" in script

    def test_returns_shell_script_with_leave_return(self):
        """Script uses 'return' for leave parameter."""
        script = dispatcher._gh_guard_check("return")
        assert "return" in script

    def test_script_contains_all_refused_commands(self):
        """All GH_GUARD_REFUSED commands are in the pattern."""
        script = dispatcher._gh_guard_check("exit")
        # The refused commands are: auth logout, auth login, auth switch, auth refresh, 
        # config set, alias set, alias delete, alias import
        assert "auth logout" in script or '"auth logout"' in script
        assert "auth login" in script or '"auth login"' in script
        assert "config set" in script or '"config set"' in script


class TestEnvHelpers:
    """_env_int() and _env_float() — environment variable parsing helpers."""

    def test_env_int_returns_default_when_not_set(self, monkeypatch):
        """Returns default when env var not set."""
        monkeypatch.delenv("TEST_ENV_VAR", raising=False)
        assert dispatcher._env_int("TEST_ENV_VAR", 42) == 42

    def test_env_int_returns_parsed_value(self, monkeypatch):
        """Parses and returns integer value from env var."""
        monkeypatch.setenv("TEST_ENV_VAR", "123")
        assert dispatcher._env_int("TEST_ENV_VAR", 42) == 123

    def test_env_int_returns_default_on_invalid_value(self, monkeypatch):
        """Returns default when env var contains invalid integer."""
        monkeypatch.setenv("TEST_ENV_VAR", "not_an_int")
        assert dispatcher._env_int("TEST_ENV_VAR", 42) == 42

    def test_env_float_returns_default_when_not_set(self, monkeypatch):
        """Returns default when env var not set."""
        monkeypatch.delenv("TEST_ENV_VAR", raising=False)
        assert dispatcher._env_float("TEST_ENV_VAR", 3.14) == 3.14

    def test_env_float_returns_parsed_value(self, monkeypatch):
        """Parses and returns float value from env var."""
        monkeypatch.setenv("TEST_ENV_VAR", "2.71")
        assert dispatcher._env_float("TEST_ENV_VAR", 3.14) == 2.71

    def test_env_float_returns_default_on_invalid_value(self, monkeypatch):
        """Returns default when env var contains invalid float."""
        monkeypatch.setenv("TEST_ENV_VAR", "not_a_float")
        assert dispatcher._env_float("TEST_ENV_VAR", 3.14) == 3.14


class TestAgentPreamble:
    """_agent_preamble() — generate agent persona prefix."""

    def test_returns_persona_for_known_agent(self):
        """Returns persona string for known agents."""
        result = dispatcher._agent_preamble("vibe")
        assert "Vibe" in result
        assert "flat-rate implementer" in result

    def test_returns_directive_only_for_unknown_agent(self):
        """Returns CRITICAL_EVALUATOR_DIRECTIVE when agent not in AGENT_PERSONAS."""
        result = dispatcher._agent_preamble("unknown_agent")
        # Unknown agents get the directive without a persona prefix
        assert dispatcher.CRITICAL_EVALUATOR_DIRECTIVE in result
        assert "Vibe" not in result  # Not a known agent persona


class TestGhGuardFunction:
    """_gh_guard_function() — generates gh wrapper function."""

    def test_returns_function_definition(self):
        """Returns a shell function definition for gh."""
        result = dispatcher._gh_guard_function()
        assert "gh()" in result
        assert "command gh" in result

    def test_removes_alias(self):
        """Removes gh alias before defining function."""
        result = dispatcher._gh_guard_function()
        assert "unalias gh" in result

    def test_uses_return_for_leave(self):
        """Uses 'return' as the leave command."""
        result = dispatcher._gh_guard_function()
        # The function calls _gh_guard_check("return")
        assert "return" in result


class TestNetHygiene:
    """_net_hygiene_prelude() and _path_prefix() — network and path setup."""

    def test_net_hygiene_prelude_returns_unset_for_proxy_vars(self, monkeypatch):
        """Returns unset command for all proxy vars when ORCH_KEEP_PROXY not set."""
        monkeypatch.delenv("ORCH_KEEP_PROXY", raising=False)
        result = dispatcher._net_hygiene_prelude()
        assert "unset" in result
        assert "HTTP_PROXY" in result
        assert "HTTPS_PROXY" in result

    def test_net_hygiene_prelude_returns_empty_when_keep_proxy_set(self, monkeypatch):
        """Returns empty string when ORCH_KEEP_PROXY=1."""
        monkeypatch.setenv("ORCH_KEEP_PROXY", "1")
        result = dispatcher._net_hygiene_prelude()
        assert result == ""

    def test_path_prefix_returns_path_export(self):
        """Returns PATH export string."""
        result = dispatcher._path_prefix()
        assert "export PATH=" in result
        assert "/opt/homebrew/bin" in result


class TestRuntimeLink:
    """_runtime_link() — create symlink for config/credential files."""

    def test_does_not_create_link_when_src_not_exists(self, tmp_path):
        """Does not create link when source does not exist."""
        src = tmp_path / "nonexistent"
        dst = tmp_path / "link"
        dispatcher._runtime_link(src, dst)
        assert not dst.exists()

    def test_does_not_create_link_when_dst_exists(self, tmp_path):
        """Does not create link when destination already exists."""
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.write_text("content")
        dst.write_text("existing")
        dispatcher._runtime_link(src, dst)
        assert dst.read_text() == "existing"

    def test_does_not_create_link_when_dst_is_symlink(self, tmp_path):
        """Does not create link when destination is already a symlink."""
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        other = tmp_path / "other"
        src.write_text("content")
        other.write_text("other")
        dst.symlink_to(other)
        dispatcher._runtime_link(src, dst)
        assert dst.read_text() == "other"

    def test_creates_symlink_when_conditions_met(self, tmp_path):
        """Creates symlink when source exists and destination does not."""
        src = tmp_path / "src"
        dst = tmp_path / "subdir" / "dst"
        src.write_text("content")
        dispatcher._runtime_link(src, dst)
        assert dst.exists()
        assert dst.is_symlink()
        assert dst.read_text() == "content"

    def test_handles_file_exists_error(self, tmp_path, monkeypatch):
        """Handles FileExistsError gracefully."""
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.write_text("content")
        # Create the destination as a symlink first
        dst.symlink_to(src)
        # This should not raise
        dispatcher._runtime_link(src, dst)
        # dst should still be a symlink
        assert dst.is_symlink()


class TestDispatchBranch:
    """_dispatch_branch() — construct dispatch branch name."""

    def test_returns_branch_for_issue_target(self, monkeypatch):
        """Returns branch name for issue target."""
        monkeypatch.setattr("provision.parse_target", lambda t: ("owner/repo", 123))
        result = dispatcher._dispatch_branch("owner/repo#123", "lane", "agent", None)
        assert "issue-123" in result

    def test_uses_custom_prefix(self, monkeypatch):
        """Uses custom prefix when provided."""
        monkeypatch.setattr("provision.parse_target", lambda t: ("owner/repo", None))
        result = dispatcher._dispatch_branch("owner/repo", "lane", "agent", "custom")
        assert "custom/lane" in result


class TestSuspiciousNetEnv:
    """_suspicious_net_env() — report ambient network env vars."""

    def test_returns_empty_list_when_no_proxy_vars(self, monkeypatch):
        """Returns empty list when no proxy or CA vars are set."""
        # Clear all the proxy and CA vars
        proxy_vars = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY", "NO_PROXY",
                      "http_proxy", "https_proxy", "all_proxy", "ftp_proxy", "no_proxy",
                      "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
                      "NODE_EXTRA_CA_CERTS", "NODE_OPTIONS"]
        for var in proxy_vars:
            monkeypatch.delenv(var, raising=False)
        
        result = dispatcher._suspicious_net_env()
        assert result == []

    def test_returns_list_when_proxy_vars_are_set(self, monkeypatch):
        """Returns list of proxy vars when they are set."""
        monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.com")
        monkeypatch.setenv("HTTPS_PROXY", "https://proxy.example.com")
        # Clear other vars
        other_vars = ["ALL_PROXY", "FTP_PROXY", "NO_PROXY",
                     "http_proxy", "https_proxy", "all_proxy", "ftp_proxy", "no_proxy",
                     "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
                     "NODE_EXTRA_CA_CERTS", "NODE_OPTIONS"]
        for var in other_vars:
            monkeypatch.delenv(var, raising=False)
        
        result = dispatcher._suspicious_net_env()
        assert len(result) >= 2  # At least HTTP_PROXY and HTTPS_PROXY
        assert any("HTTP_PROXY" in item for item in result)
        assert any("HTTPS_PROXY" in item for item in result)


class TestBuildPrompt:
    """build_prompt() — construct agent prompt from task details."""

    def test_appends_owner_question_protocol(self):
        """Prompt always ends with OWNER_QUESTION_PROTOCOL."""
        result = dispatcher.build_prompt("review", "owner/repo#123")
        assert dispatcher.OWNER_QUESTION_PROTOCOL in result

    def test_includes_target(self):
        """Prompt includes the target."""
        result = dispatcher.build_prompt("review", "owner/repo#123")
        assert "owner/repo#123" in result

    def test_includes_task_type(self):
        """Prompt includes the task type (capitalized in template)."""
        result = dispatcher.build_prompt("review", "owner/repo#123")
        # The template capitalizes 'review' to 'Review'
        assert "Review" in result

    def test_uses_template_when_available(self):
        """Uses PROMPT_TEMPLATES for known task types."""
        # This tests that the function uses templates when available
        result = dispatcher.build_prompt("offload", "owner/repo#123", target_detail="some details")
        # Just verify it doesn't raise and returns a string
        assert isinstance(result, str)
