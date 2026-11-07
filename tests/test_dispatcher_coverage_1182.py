"""Ranked coverage targets for issue #1182 - dispatcher.py coverage.

This test file adds coverage for dispatcher.py functions identified by the
coverage-gap ranking in docs/verification/pr-710-dispatcher-planning/ranking.json.
Target: src/dispatcher.py (tier1_escaped_defects: 45.0, tier2_churn: 64, tier3_uncovered_statements: 0)
"""

from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

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


@pytest.fixture(autouse=True)
def isolated_dispatcher(tmp_path, monkeypatch):
    """No test in this module may reach live state, a process, or the network."""
    for name in ("ORCH_STATE_DIR", "ORCH_LOCAL_RUNTIME"):
        monkeypatch.setenv(name, str(tmp_path))
    monkeypatch.setattr(dispatcher, "OFFLOAD_DIR", tmp_path / "offloads")
    monkeypatch.setattr(dispatcher, "AGENT_RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(dispatcher, "REAL_HOME", tmp_path / "home")
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        pytest.fail("unexpected process or network call")

    monkeypatch.setattr(dispatcher.subprocess, "run", forbidden)
    monkeypatch.setattr(dispatcher.subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    yield
    assert calls == []


def test_prompt_without_template_keeps_owner_decisions(monkeypatch):
    monkeypatch.setattr(dispatcher.repo_knowledge, "append_context", lambda *a, **k: "context")
    monkeypatch.setattr(
        dispatcher.feedback,
        "owner_decisions_for",
        lambda **k: [{"question": "Publish?", "decision": "Wait"}],
    )
    prompt = dispatcher.build_prompt("unregistered-task", "owner/repo#1", "detail")
    assert prompt.startswith("Work owner/repo#1 (unregistered-task). context")
    assert "- Publish? -> Wait" in prompt
    assert prompt.endswith(dispatcher.OWNER_QUESTION_PROTOCOL)


def test_capability_matching_failure_does_not_block_prompt(monkeypatch):
    import capabilities

    def broken(*args, **kwargs):
        raise RuntimeError("fixture heartbeat unavailable")

    heartbeat = Mock(side_effect=broken)
    monkeypatch.setattr(capabilities, "production_heartbeat", heartbeat)
    monkeypatch.setattr(dispatcher.repo_knowledge, "append_context", lambda *a, **k: "context")
    monkeypatch.setattr(dispatcher.feedback, "owner_decisions_for", lambda **k: [])
    assert "context" in dispatcher.build_prompt("epic", "owner/repo#1")
    heartbeat.assert_called_once()


def test_unbounded_git_probe_timeout_is_not_a_worktree(monkeypatch, tmp_path):
    probe = Mock(side_effect=dispatcher.subprocess.TimeoutExpired("git", 5))
    monkeypatch.setattr(dispatcher.subprocess, "run", probe)
    assert dispatcher._is_git_worktree(tmp_path) is False
    assert probe.call_count == 1
    assert probe.call_args.kwargs["timeout"] is None


@pytest.mark.parametrize("present", [False, True])
def test_partial_cleanup_requires_the_original_inode(tmp_path, present):
    partial = tmp_path / "partial"
    if present:
        partial.mkdir()
    assert dispatcher._remove_owned_offload_partial(partial, (-1, -1)) is (not present)
    assert partial.exists() is present


@pytest.mark.parametrize("state", ["running", "absent", "unreadable", "denied", "alive"])
def test_copier_terminal_requires_absent_group(monkeypatch, state):
    child = SimpleNamespace(pid=12345, poll=lambda: None if state == "running" else 0)
    errors = {"absent": ProcessLookupError, "unreadable": OSError, "denied": PermissionError}
    probe = Mock(side_effect=errors[state]("fixture") if state in errors else None)
    monkeypatch.setattr(dispatcher.os, "killpg", probe)
    if state == "denied":
        with pytest.raises(PermissionError):
            dispatcher._offload_copier_terminal(child)
    else:
        assert dispatcher._offload_copier_terminal(child) is (state == "absent")
    assert probe.call_count == (0 if state == "running" else 1)


@pytest.mark.parametrize("expired", [False, True])
def test_workspace_copy_refuses_before_creating_output(tmp_path, monkeypatch, expired):
    monkeypatch.setattr(dispatcher.adapters, "workspace_path", lambda path: Path(path))
    error = TimeoutError if expired else FileNotFoundError
    with pytest.raises(error):
        dispatcher._isolate_offload_cwd(tmp_path / "absent", timeout=0 if expired else 1)
    assert not dispatcher.OFFLOAD_DIR.exists()


def test_vibe_config_replaces_only_session_logging_save_dir(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_text('[session_logging]\nsave_dir = "old"\nenabled = false\n[other]\nx = 1\n')
    dispatcher._write_vibe_runtime_config(source, target, tmp_path / "logs")
    assert target.read_text() == (
        f'[session_logging]\nsave_dir = "{tmp_path / "logs"}"\n' "enabled = false\n[other]\nx = 1\n"
    )
    assert 'save_dir = "old"' in source.read_text()


def test_guard_cleanup_error_preserves_original_write_error(tmp_path, monkeypatch):
    target = tmp_path / "guard" / "gh"
    monkeypatch.setattr(dispatcher, "_gh_guard_files", lambda guard: {target: "new"})
    monkeypatch.setattr(dispatcher.os, "replace", Mock(side_effect=PermissionError("replace")))
    unlink = Mock(side_effect=OSError("unlink"))
    monkeypatch.setattr(dispatcher.os, "unlink", unlink)
    with pytest.raises(PermissionError, match="replace"):
        dispatcher._ensure_gh_guard()
    assert unlink.call_count == 1
    assert not target.exists()


@pytest.mark.parametrize(
    "failure", ["lineage", "lookup", "missing-binding", "kind", "invoke", "receipt"]
)
def test_compiled_capability_refuses_incomplete_evidence(monkeypatch, failure):
    import capabilities
    import capability_lifecycle
    import capability_targets

    plan = {
        "target": "owner/repo#1",
        "run_id": "fixture",
        "routing_metadata": {
            "selected_capability_id": "compiled",
            "selected_capability_version_id": "v1",
        },
        "capability_ids": ["compiled", "base"],
        "capability_version_ids": ["v1", "b1"],
    }
    load = Mock(return_value={"compiled": {"capability_version_id": "v1"}})
    binding = Mock(return_value={"kind": "workflow"})
    invoke = Mock(return_value={"invoked": True, "accepted": True})
    monkeypatch.setattr(capabilities, "load", load)
    monkeypatch.setattr(capability_targets, "get_binding", binding)
    monkeypatch.setattr(capability_lifecycle, "invoke_compiled_target", invoke)
    if failure == "lineage":
        plan["capability_version_ids"] = ["v1"]
        with pytest.raises(ValueError, match="lineage length mismatch"):
            dispatcher._invoke_selected_compiled_capability(plan)
        load.assert_not_called()
        invoke.assert_not_called()
        return
    if failure == "lookup":
        load.side_effect = OSError("fixture registry")
    elif failure == "missing-binding":
        binding.return_value = None
    elif failure == "kind":
        binding.return_value = {"kind": "unsupported"}
    elif failure == "invoke":
        invoke.side_effect = RuntimeError("fixture invocation")
    reasons = {
        "lookup": "target_lookup_failed:OSError",
        "missing-binding": "target_binding_missing",
        "kind": "unsupported_target_kind",
        "invoke": "invoke_failed:RuntimeError",
        "receipt": "consumption_receipt_missing",
    }
    result = dispatcher._invoke_selected_compiled_capability(plan)
    assert result == {
        "invoked": failure == "receipt",
        "accepted": False,
        "reason": reasons[failure],
    }
    assert plan["capability_ids"] == ["base"]
    assert plan["capability_version_ids"] == ["b1"]
    assert plan["routing_metadata"]["capability_fallback"] == "baseline"
    assert invoke.call_count == (1 if failure in {"invoke", "receipt"} else 0)


@pytest.mark.parametrize(
    "failure",
    ["exit", "pages", "rest-page", "body", "pagination", "cursor", "nodes", "refs", "repo"],
)
def test_linked_pr_read_keeps_malformed_evidence_unknown(monkeypatch, failure):
    pr = {"number": 2, "body": "Closes #1", "head": {"ref": "topic"}}
    refs = {
        "pageInfo": {"hasNextPage": False},
        "nodes": [{"number": 1, "repository": {"nameWithOwner": "owner/repo"}}],
    }
    connection = {
        "pageInfo": {"hasNextPage": False},
        "nodes": [{"number": 2, "closingIssuesReferences": refs}],
    }
    rest = [[pr]]
    graph = [{"data": {"repository": {"pullRequests": connection}}}]
    expected = {
        "exit": "gh exit 1",
        "pages": "missing GitHub response pages",
        "rest-page": "invalid REST PR page",
        "body": "invalid PR body or head branch",
        "pagination": "invalid GraphQL pagination",
        "cursor": "invalid GraphQL cursor",
        "nodes": "invalid GraphQL PR nodes",
        "refs": "invalid closing reference nodes",
        "repo": "invalid closing reference repository",
    }
    if failure == "pages":
        rest = []
    elif failure == "rest-page":
        rest = [{}]
    elif failure == "body":
        pr["body"] = 123
    elif failure == "pagination":
        connection["pageInfo"]["hasNextPage"] = 1
    elif failure == "cursor":
        connection["pageInfo"].update(hasNextPage=True, endCursor="")
        graph.append(graph[0])
    elif failure == "nodes":
        connection["nodes"] = {}
    elif failure == "refs":
        refs["nodes"] = {}
    elif failure == "repo":
        refs["nodes"][0]["repository"]["nameWithOwner"] = 123
    responses = [
        SimpleNamespace(
            returncode=1 if failure == "exit" else 0, stdout=json.dumps(rest), stderr="fixture"
        ),
        SimpleNamespace(returncode=0, stdout=json.dumps(graph), stderr=""),
    ]
    read = Mock(side_effect=responses)
    monkeypatch.setattr(dispatcher.subprocess, "run", read)
    assert dispatcher.linked_open_prs("owner/repo#1") is None
    assert expected[failure] in dispatcher._linked_pr_read_error.get()
    assert read.call_count == (1 if failure in {"exit", "pages", "rest-page", "body"} else 2)


@pytest.mark.parametrize("source", [None, "empty", "large", "valid"])
def test_controlled_source_digest_requires_bounded_file(tmp_path, source):
    if source is None:
        with pytest.raises(ValueError, match="identity unavailable"):
            dispatcher._controlled_module_sha(SimpleNamespace())
        return
    path = tmp_path / "module.py"
    raw = b"" if source == "empty" else b"x" * (8_000_001 if source == "large" else 5)
    path.write_bytes(raw)
    if source == "valid":
        assert (
            dispatcher._controlled_module_sha(SimpleNamespace(__file__=str(path)))
            == hashlib.sha256(raw).hexdigest()
        )
    else:
        with pytest.raises(ValueError, match="exceeds bound"):
            dispatcher._controlled_module_sha(SimpleNamespace(__file__=str(path)))


def test_controlled_cleanup_does_not_retry_an_uncertain_removal(tmp_path, monkeypatch):
    context = SimpleNamespace(root=tmp_path / "owned", runtime_cleanup_attempted=False)
    remove = Mock(side_effect=PermissionError("fixture"))
    monkeypatch.setattr(dispatcher.shutil, "rmtree", remove)
    result = {}
    dispatcher._controlled_cleanup_result(context, result)
    dispatcher._controlled_cleanup_result(context, result)
    remove.assert_called_once_with(context.root)
    assert result["retry_safe"] is False
    assert result["semantic_promotion_blocked"] is True
    assert result["data_only_private_runtime"]["cleanup"] == "held_owned_cleanup_unverified"


@pytest.mark.parametrize(
    "observation", [None, {"provider_argv": ["secret"], "transport_argv": [1], "pid": 42}]
)
def test_public_observation_hides_raw_argv(observation):
    result = dispatcher._public_process_observation(observation)
    if observation is None:
        assert result is None
    else:
        assert result == {
            "pid": 42,
            "provider_argv_argc": 1,
            "provider_argv_sha256": hashlib.sha256(b"secret\0").hexdigest(),
        }
        assert observation["provider_argv"] == ["secret"]


def test_remote_label_success_survives_feedback_failure(monkeypatch):
    apply = Mock(return_value=SimpleNamespace(returncode=0, stderr=""))
    record = Mock(side_effect=OSError("fixture feedback"))
    monkeypatch.setattr(dispatcher.subprocess, "run", apply)
    monkeypatch.setattr(dispatcher.feedback, "record_run", record)
    monkeypatch.setattr(dispatcher.adapters, "model_identity", lambda *args: "fixture-model")
    result = dispatcher.delegate_remote("codex", "owner/repo#1", labels=set())
    assert result["applied"] is True
    assert result["labels_read"] is True
    assert result["stderr"] == ""
    assert apply.call_count == record.call_count == 1
    assert apply.call_args.args[0][-1] == "labels[]=agent:codex"


@pytest.mark.parametrize("value", [True, 0, -1, "2"])
def test_linked_pr_number_must_be_a_positive_integer(monkeypatch, value):
    read = Mock(
        return_value=SimpleNamespace(
            returncode=0, stderr="", stdout=json.dumps([[{"number": value}]])
        )
    )
    monkeypatch.setattr(dispatcher.subprocess, "run", read)
    assert dispatcher.linked_open_prs("owner/repo#1") is None
    assert dispatcher._linked_pr_read_error.get() == "invalid PR/issue number"
    read.assert_called_once()


@pytest.mark.parametrize("extra", [{"pid": True}, {"os_started": ""}, {"finite_frontier_id": None}])
def test_finite_pool_fence_rejects_untrusted_identity(extra):
    observation = {
        "source": "trusted-popen-transport/v1",
        "cleanup_terminal": False,
        "pid": 42,
        "process_group": 42,
        "os_started": "fixture birth",
        "run_id": "fixture",
        "finite_frontier_id": "frontier",
        "finite_frontier_target": "owner/repo#1",
    }
    assert dispatcher._finite_pool_fence({**observation, **extra}) is None
    valid = dispatcher._finite_pool_fence(observation)
    assert valid["process_group"] == 42
    assert valid["cleanup_terminal"] is False


@pytest.mark.parametrize("review_round", [True, "1", 0, -1, 1000000])
def test_invalid_review_round_refuses_before_claiming(monkeypatch, review_round):
    claim = Mock(side_effect=AssertionError("must not claim"))
    monkeypatch.setattr(dispatcher.claims, "claim", claim)
    result = dispatcher.delegate(
        "codex",
        "owner/repo#1",
        "closer",
        "fixture prompt",
        review_loop=True,
        review_round=review_round,
    )
    assert result == {"error": "review round outside bounded review loop"}
    claim.assert_not_called()
