"""The keepalive ingest's PR listing must never read a failed `gh pr list` as an empty repo, must
not request `commits` in the list (GitHub's node limit fails the whole query at --limit 300, which
silenced the ingest from 2026-09-17 to 2026-09-20), and must fetch commit identities in batches only
for the PRs the cheap resolvers leave unattributed."""

from __future__ import annotations

import pytest

import feedback
import keepalive_outcomes as k


@pytest.fixture
def brain(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "t.db")
    return tmp_path


def _pr(number, *, head, author="someone-else", labels=()):
    return {
        "number": number,
        "state": "MERGED",
        "title": f"Change {number}",
        "labels": [{"name": lab} for lab in labels],
        "createdAt": "2026-09-18T10:00:00Z",
        "updatedAt": "2026-09-18T12:00:00Z",
        "mergedAt": "2026-09-18T12:00:00Z",
        "closedAt": "2026-09-18T12:00:00Z",
        "headRefName": head,
        "baseRefName": "main",
        "mergeCommit": {"oid": "abc123"},
        "author": {"login": author},
        "body": "",
        "url": f"https://github.com/o/r/pull/{number}",
    }


def test_pr_list_fields_carry_no_commits():
    """`gh pr list --json …,commits --limit 300` fails with "requesting up to 1,000,000 possible
    nodes which exceeds the maximum limit of 500,000"; the failure read as zero PRs for three days.
    """
    assert "commits" not in k.PR_LIST_FIELDS.split(",")
    assert "headRefName" in k.PR_LIST_FIELDS.split(",")  # the branch-prefix resolver's input stays


def test_fetch_failure_is_named_not_counted_as_empty(brain):
    failed = k.ingest_keepalive_outcomes(
        ["o/r"],
        dry_run=True,
        _pr_fetch_fn=lambda repo, days: None,
        _closure_context_fn=lambda r, n: "",
    )
    assert failed["fetch_failed_repos"] == ["o/r"] and failed["prs_seen"] == 0
    empty = k.ingest_keepalive_outcomes(
        ["o/r"],
        dry_run=True,
        _pr_fetch_fn=lambda repo, days: [],
        _closure_context_fn=lambda r, n: "",
    )
    assert empty["fetch_failed_repos"] == [] and empty["prs_seen"] == 0


def test_unresolved_prs_get_commit_identities_from_the_batch_only(brain):
    calls: list = []

    def evidence(repo, numbers):
        calls.append((repo, list(numbers)))
        return {7: {"commit_identities": ["claude", "noreply@anthropic.com"]}}

    prs = [
        _pr(7, head="feature/no-prefix"),  # nothing cheap resolves it
        _pr(9, head="codex/issue-9-thing"),  # the branch prefix resolves it
    ]
    summary = k.ingest_keepalive_outcomes(
        ["o/r"],
        dry_run=True,
        _pr_fetch_fn=lambda repo, days: prs,
        _closure_context_fn=lambda r, n: "",
        _evidence_fetch_fn=evidence,
    )
    assert calls == [("o/r", [7])], "only the unattributed PR is sent to the GraphQL batch"
    assert summary["commit_identity_enriched"] == 1
    assert summary["prs_seen"] == 2 and summary["runs_recorded"] == 2
    assert summary["attribution"]["by_source"].get("commit_identity") == 1
    assert summary["attribution"]["by_source"].get("branch_prefix") == 1


def test_paused_review_repos_are_ingested_and_ignored_ones_are_not(tmp_path):
    """Review cadence and PR evidence are different questions: a paused review still merges PRs."""
    import json

    import keepalive_outcomes as ko

    reg = tmp_path / "repo_review_registry.json"
    reg.write_text(
        json.dumps(
            {
                "repos": [
                    {"repo": "stranske/A", "status": "active"},
                    {"repo": "stranske/B", "status": "paused"},
                    {"repo": "stranske/C", "status": "ignored"},
                    {"status": "active"},
                ]
            }
        )
    )
    assert ko._active_repos(reg) == ["stranske/A", "stranske/B"]
