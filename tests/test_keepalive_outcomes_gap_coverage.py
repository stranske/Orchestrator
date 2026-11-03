import json
import subprocess
from types import SimpleNamespace

import pytest

import feedback
import keepalive_outcomes as outcomes


def test_registry_and_gh_read_helpers_fail_open_without_starting_processes(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "repos": [
                    {"repo": "o/active", "status": "active"},
                    {"repo": "o/paused", "status": "paused"},
                    {"repo": "o/ignored", "status": "ignored"},
                ]
            }
        )
    )
    monkeypatch.setenv("ORCH_REPO_REVIEW_REGISTRY", str(registry))
    assert outcomes._resolve_registry_path() == registry
    assert outcomes._active_repos() == ["o/active", "o/paused"]
    with pytest.raises(RuntimeError, match="repo_review_registry unreadable"):
        outcomes._active_repos(tmp_path / "missing.json")

    calls = []
    monkeypatch.setattr(
        outcomes.subprocess,
        "run",
        lambda args, **kwargs: calls.append((args, kwargs))
        or SimpleNamespace(returncode=0, stdout="[{}]"),
    )
    assert outcomes._run_json(["gh", "x"], timeout=9) == [{}]
    assert calls[0][1]["timeout"] == 9
    monkeypatch.setattr(
        outcomes.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="[]"),
    )
    assert outcomes._run_json(["gh"]) is None
    monkeypatch.setattr(
        outcomes.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("gh", 1)),
    )
    assert outcomes._run_json(["gh"]) is None


def test_fetch_and_attribution_helpers_keep_malformed_data_unattributed(monkeypatch):
    seen = []
    monkeypatch.setattr(
        outcomes, "_run_json", lambda args, **kwargs: seen.append(args) or [{"number": 1}]
    )
    assert outcomes._fetch_prs("o/r", 2, now=172800) == [{"number": 1}]
    assert "updated:>=1970-01-01" in seen[0]
    monkeypatch.setattr(
        outcomes,
        "_run_json",
        lambda *args, **kwargs: {"body": "text", "comments": [{"body": "note"}, "plain", {}]},
    )
    assert outcomes._fetch_pr_context("o/r", 4) == "text\nnote\nplain"
    assert outcomes.process_suppression_reason("process_ignore=Duplicate") == "duplicate"
    assert (
        outcomes.process_suppression_reason("Closing as a duplicate")
        == outcomes.PROCESS_IGNORE_DUPLICATE_OR_SUPERSEDED
    )
    assert (
        outcomes._with_process_ignore_note({"notes": "x"}, "why")["notes"]
        == "x; process_ignore=why"
    )
    assert (
        outcomes._with_process_ignore_note({"notes": "process_ignore=why"}, "why")["notes"]
        == "process_ignore=why"
    )
    assert outcomes._task_type_from_labels(["Type: review"]) == "review"
    assert outcomes._task_type_from_labels(["bug"]) == "fix"
    assert outcomes._task_type_from_labels(["documentation"]) == "docs"
    assert outcomes._task_type_from_labels(["tests"]) == "test"
    assert outcomes._task_type_from_labels([]) == "implement"
    assert outcomes._created_epoch({"createdAt": "bad"}) is None
    assert outcomes._created_epoch({"createdAt": "1970-01-01T00:00:01Z"}) == 1
    assert outcomes._commit_identities(
        {
            "commits": [
                None,
                {
                    "authors": [{"name": "A", "user": {"login": "B"}}],
                    "messageBody": "Co-authored-by: C",
                },
            ]
        }
    ) == ["a", "b", "co-authored-by: c"]
    assert outcomes.derive_attribution(["agent: codex"]) == ("codex", "agent_label")
    assert outcomes.derive_attribution(["agents:tried-claude"]) == ("claude", "tried_label")
    assert outcomes.derive_attribution([], job_names=["keepalive next task (cursor)"]) == (
        "cursor",
        "keepalive_job",
    )
    assert outcomes.derive_attribution([], summary="automated status summary\nAgent: gemini") == (
        "gemini",
        "automated_status_summary",
    )
    assert outcomes.derive_attribution([], head_ref="sync/workflows-delivery") == (
        "none",
        "bot:template-sync",
    )
    assert outcomes.derive_attribution([], commit_identities=["tim stranske"]) == ("none", "human")


def test_database_helpers_and_outcome_replacement_are_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    feedback.record_run(
        "remote:o/r#2:x",
        "o/r#2",
        "x",
        "codex",
        mode="remote",
        pr_number=2,
        source="orchestrator_remote",
    )
    assert outcomes._existing_remote_for_pr("o/r", 2) == "remote:o/r#2:x"
    assert outcomes._existing_remote_for_pr("o/r", 3) is None
    assert outcomes._source_counts()["orchestrator_remote"] == 1
    assert outcomes._should_record_outcome(None, {"durability": "pending"})
    assert outcomes._should_record_outcome({"durability": "pending"}, {"durability": "durable"})
    assert not outcomes._should_record_outcome({"durability": "durable"}, {"durability": "durable"})
    counts = {}
    monkeypatch.setattr(outcomes, "_gh_throttle", lambda _: None)
    monkeypatch.setattr(
        outcomes.outcomes,
        "attach_keepalive_replacement",
        lambda pr, *args, **kwargs: {**pr, "state": "OPEN"},
    )
    monkeypatch.setattr(
        outcomes.outcomes,
        "count_keepalive_replacement",
        lambda target, judged: target.update(read=True),
    )
    monkeypatch.setattr(outcomes.outcomes, "state_to_outcome", lambda pr: {"state": pr["state"]})
    result = outcomes._with_replacement_read(
        "o/r", {"number": 2, "state": "CLOSED"}, {"state": "CLOSED"}, None, object(), 1, counts
    )
    assert result == {"state": "OPEN"} and counts == {"read": True}
    assert outcomes._with_replacement_read(
        "o/r", {"state": "OPEN"}, {"x": 1}, None, None, 1, {}
    ) == {"x": 1}
